#!/usr/bin/env python

# also dynamically imports ansible in code

import argparse
import configparser
import contextlib
import functools
import glob
import itertools
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import yaml

from collections import defaultdict
from collections.abc import Mapping
from importlib import import_module
from string import Template
from typing import Any, Dict, Iterable, Set, Union

from ansible.parsing.yaml.dumper import AnsibleDumper
from ansible.parsing.yaml.loader import AnsibleLoader
from ansible.vars.reserved import is_reserved_name

import logzero
from logzero import logger

from baron.parser import ParsingError
import redbaron

import backoff

from gh import GitHubOrgClient
from rsa_utils import RSAKey
from template_utils import render_template_into


# CONSTANTS/SETTINGS

# https://github.com/ansible/ansible/blob/100fe52860f45238ee8ca9e3019d1129ad043c68/hacking/fix_test_syntax.py#L62
FILTER_RE = re.compile(r'((.+?)\s*([\w \.\'"]+)(\s*)\|(\s*)(\w+))')
TEST_RE = re.compile(r'((.+?)\s*([\w \.\'"]+)(\s*)is(\s*)(\w+))')

DEVEL_URL = 'https://github.com/ansible/ansible.git'
DEVEL_BRANCH = 'devel'

ALL_THE_FILES = set()

COLLECTION_NAMESPACE = 'test_migrate_ns'
PLUGIN_EXCEPTION_PATHS = {'modules': 'lib/ansible/modules', 'module_utils': 'lib/ansible/module_utils', 'inventory_scripts': 'contrib/inventory'}
PLUGIN_DEST_EXCEPTION_PATHS = {'inventory_scripts': 'scripts/inventory'}

COLLECTION_SKIP_REWRITE = ('_core',)

RAW_STR_TMPL = "r'''{str_val}'''"
STR_TMPL = "'''{str_val}'''"

BAD_EXT = frozenset({'.pyo', '.pyc'})

VALID_SPEC_ENTRIES = frozenset({
    'action',
    'become',
    'cache',
    'callback',
    'cliconf',
    'connection',
    'doc_fragments',
    'filter',
    'httpapi',
    'inventory',
    'lookup',
    'module_utils',
    'modules',
    'netconf',
    'shell',
    'strategy',
    'terminal',
    'test',
    'vars',
    'inventory_scripts',
})

NOT_PLUGINS = frozenset(set(['inventory_scripts']))

VARNAMES_TO_PLUGIN_MAP = {
    'ansible_become_method': 'become',
    'ansible_connection': 'connection',
    'ansible_shell_type': 'shell',
}

KEYWORDS_TO_PLUGIN_MAP = {
    'become_method': 'become',
    'cache_plugin': 'cache',
    'connection': 'connection',
    'plugin': 'inventory',
    'strategy': 'strategy',
}

REWRITE_CLASS_PROPERTY_MAP = {
    'BecomeModule': 'name',
    'CallbackModule': 'CALLBACK_NAME',
    'Connection': 'transport',
    'InventoryModule': 'NAME',
}

REWRITE_CLASS_PROPERTY_PLUGINS= {
    'become',
    'callback',
    'connection',
    'inventory',
}

VARDIR = os.environ.get('GRAVITY_VAR_DIR', '.cache')
LOGFILE = os.path.join(VARDIR, 'errors.log')

REMOVE = set()

core = {}
manual_check = defaultdict(list)


### FUNCTION DEFS

def _is_unexpected_error(proc_err):
    err_out = proc_err.stderr
    return not (
        err_out.startswith('ERROR: Permission to ')
        and ' denied to deploy key' in err_out
    )


retry_on_permission_denied = backoff.on_exception(  # pylint: disable=invalid-name
    backoff.expo, subprocess.CalledProcessError,
    max_time=8, jitter=backoff.full_jitter,
    giveup=_is_unexpected_error,
)


@retry_on_permission_denied
def ensure_cmd_succeeded(ssh_agent, cmd, cwd):
    """Perform cmd"""
    cmd_out = ssh_agent.check_output(cmd, stderr=subprocess.PIPE, cwd=cwd, text=True)
    print(cmd_out)


class UnmovablePathStr(str): ...


def add_core(ptype, name):

    global core
    if ptype not in core:
        core[ptype] = set()

    core[ptype].add(name)


def add_manual_check(key, value, filename):
    global manual_check
    manual_check[filename].append((key, value))


def checkout_repo(
        git_url: str, checkout_path: str,
        *,
        refresh: bool = False,
) -> Set[str]:
    """Fetch and optionally refresh the repo."""
    if not os.path.exists(checkout_path):
        subprocess.check_call(('git', 'clone', git_url, checkout_path))
    elif refresh:
        subprocess.check_call(('git', 'checkout', DEVEL_BRANCH), cwd=checkout_path)
        subprocess.check_call(('git', 'pull', '--rebase'), cwd=checkout_path)

    return set(
        f.strip()
        for f in subprocess.check_output(
            ('git', '-c', 'core.quotepath=false', 'ls-tree', '--full-tree', '-r', '--name-only', 'HEAD'),
            text=True, cwd=checkout_path,
        ).split('\n')
        if f.strip()
    )


### FILE utils

def remove(path):
    global REMOVE
    REMOVE.add(path)


def actually_remove(checkout_path, namespace, collection):
    global REMOVE

    # load sanity/ignore.txt, the files being removed below need to be removed from ignore.txt too
    sanity_ignore = read_lines_from_file(os.path.join(checkout_path, 'test/sanity/ignore.txt'))
    new_sanity_ignore = defaultdict(list)
    for ignore in sanity_ignore:
        values = ignore.split(' ', 1)
        new_sanity_ignore[values[0]].append(values[1])

    # actually remove files we marked for removal
    init_files = set()
    for path in REMOVE:
        actual_devel_path = os.path.relpath(path, checkout_path)
        if actual_devel_path.startswith('lib/ansible/modules') and actual_devel_path.endswith('__init__.py'):
            init_files.add(path)
            continue

        subprocess.check_call(('git', 'rm', actual_devel_path), cwd=checkout_path)
        new_sanity_ignore.pop(actual_devel_path, None)

    # process the __init__.py files from module dirs now that all files are removed,
    # that way we can check if there are no modules left in the dirs they are in
    # so we can remove __init__.py as well
    for init in sorted(init_files, key=lambda x: len(x.split('/')), reverse=True):
        if os.listdir(os.path.dirname(init)) != ['__init__.py']:
            continue

        actual_devel_path = os.path.relpath(init, checkout_path)
        subprocess.check_call(
            ('git', 'rm', actual_devel_path),
            cwd=checkout_path,
        )
        new_sanity_ignore.pop(actual_devel_path, None)

    # save modified sanity/ignore.txt
    res = ''
    for filename, values in new_sanity_ignore.items():
        for value in values:
            # value contains '\n' which is preserved from the original file
            res += '%s %s' % (filename, value)

    write_text_into_file(os.path.join(checkout_path, 'test/sanity/ignore.txt'), res)
    subprocess.check_call(('git', 'add', 'test/sanity/ignore.txt'), cwd=checkout_path)

    # commit the changes
    subprocess.check_call(('git', 'commit', '-m', 'Migrated to %s.%s' % (namespace, collection), '--allow-empty'), cwd=checkout_path)


def read_yaml_file(path):
    with open(path, 'rb') as yaml_file:
        return yaml.safe_load(yaml_file)


def read_ansible_yaml_file(path):
    with open(path, 'rb') as yaml_file:
        return AnsibleLoader(yaml_file.read(), file_name=path).get_single_data()


def write_yaml_into_file_as_is(path, data):
    yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    write_text_into_file(path, yaml_text)


def write_ansible_yaml_into_file_as_is(path, data):
    yaml_text = yaml.dump(data, Dumper=AnsibleDumper, allow_unicode=True, default_flow_style=False, sort_keys=False)
    write_text_into_file(path, yaml_text)


def read_text_from_file(path):
    with open(path, 'r') as f:
        return f.read()


def read_lines_from_file(path):
    with open(path, 'r') as f:
        return f.readlines()


def write_text_into_file(path, text):
    with open(path, 'w') as f:
        return f.write(text)


@contextlib.contextmanager
def working_directory(target_dir):
    """Temporary change dir to the target and change back on exit."""
    current_working_dir = os.getcwd()
    os.chdir(target_dir)
    try:
        yield os.getcwd()
    finally:
        os.chdir(current_working_dir)


### SPEC UTILS

def load_spec_file(spec_file):
    spec = read_yaml_file(spec_file)  # TODO: capture yamlerror?

    if not isinstance(spec, Mapping):
        sys.exit("Invalid format for spec file, expected a dictionary and got %s" % type(spec))
    elif not spec:
        sys.exit("Cannot use spec file, ended up with empty spec")

    return spec


def resolve_spec(spec, checkoutdir):
    # TODO: add negation? entry: x/* \n entry: !x/base.py
    for ns in spec.keys():
        for coll in spec[ns].keys():
            for ptype in spec[ns][coll].keys():
                if ptype not in VALID_SPEC_ENTRIES:
                    raise Exception('Invalid plugin type: %s, expected one of %s' % (ptype, VALID_SPEC_ENTRIES))
                plugin_base = os.path.join(checkoutdir, PLUGIN_EXCEPTION_PATHS.get(ptype, os.path.join('lib', 'ansible', 'plugins', ptype)))
                replace_base = '%s/' % plugin_base
                for entry in spec[ns][coll][ptype]:
                    if r'*' in entry or r'?' in entry:
                        files = glob.glob(os.path.join(plugin_base, entry))
                        if not files:
                            raise Exception('No matches for plugin type: %s, entry: %s. Searched in %s.' % (ptype, entry, os.path.join(plugin_base, entry)))

                        for fname in files:
                            if ptype != 'module_utils' and fname.endswith('__init__.py') or not os.path.isfile(fname):
                                continue
                            fname = fname.replace(replace_base, '')
                            spec[ns][coll][ptype].insert(0, fname)

                        # clean out glob entry
                        spec[ns][coll][ptype].remove(entry)

                def dir_to_path(path):
                    if not (os.path.isdir(path) and os.path.exists(path)):
                        return path
                    return next(
                        subpath
                        for subpath in glob.glob(os.path.join(path, '**'), recursive=True)
                        if not os.path.isdir(subpath)
                    )
                logger.info(
                    'Verifying that all %s '
                    'that are scheduled for migration '
                    'to %s.%s exist...',
                    ptype, ns, coll,
                )
                assert_migrating_git_tracked_resources(
                    os.path.relpath(dir_to_path(p_abs), checkoutdir)
                    for p in spec[ns][coll][ptype]
                    for p_abs in (
                        glob.glob(os.path.join(plugin_base, p))
                        or [os.path.join(plugin_base, p)]
                    )
                )
                logger.info(
                    'All %s entries for %s.%s '
                    'are valid',
                    ptype, ns, coll,
                )


### GET_PLUGINS UTILS

def get_plugin_collection(plugin_name, plugin_type, spec):
    for ns in spec.keys():
        for collection in spec[ns].keys():
            if spec[ns][collection]: # avoid empty collections
                plugins = spec[ns][collection].get(plugin_type, [])
                if plugin_name + '.py' in plugins:
                    return ns, collection

    # keep info
    plugin_name = plugin_name.replace('/', '.')
    logger.debug('Assuming "%s.%s" stays in core', plugin_type, plugin_name)
    add_core(plugin_type, plugin_name.replace('/', '.'))

    raise LookupError('Could not find "%s" named "%s" in any collection in the spec' % (plugin_type, plugin_name))


def get_plugins_from_collection(ns, collection, plugin_type, spec):
    assert ns in spec
    assert collection in spec[ns]
    return [plugin.rsplit('/')[-1][:-3] for plugin in spec[ns][collection].get(plugin_type, [])]


def get_plugin_fqcn(namespace, collection, plugin_name):
    return '%s.%s.%s' % (namespace, collection, plugin_name)


def get_rewritable_collections(namespace, spec):
    return (collection for collection in spec[namespace].keys() if collection not in COLLECTION_SKIP_REWRITE)


### REWRITE FUNCTIONS

def rewrite_class_property(mod_fst, collection, namespace, filename):
    if all(f'plugins/{p}' not in filename for p in REWRITE_CLASS_PROPERTY_PLUGINS):
        return

    for class_name, property_name in REWRITE_CLASS_PROPERTY_MAP.items():
        try:
            val = (
                mod_fst.
                find('class', name=class_name).
                find('name', value=property_name).parent.value
            )
        except AttributeError:
            continue

        try:
            val.value = "'%s'" % get_plugin_fqcn(namespace, collection, val.to_python())
        except ValueError:
            # so this might be something like:
            # transport = CONNECTION_TRANSPORT
            add_manual_check(property_name, val.value, filename)


def normalize_implicit_relative_imports_in_unit_tests(mod_fst, file_path):
    """Locate implicit imports and prepend them with dot."""
    cur_pkg_dir = os.path.dirname(file_path)
    make_pkg_subpath = functools.partial(os.path.join, cur_pkg_dir)
    for imp in mod_fst.find_all(('from_import', )):
        if not imp.value:  # from . import something
            continue

        *pkg_path_parts, pkg_or_mod = tuple(t.value for t in imp.value)
        if (
                (pkg_path_parts and not pkg_path_parts[0])
                or (not pkg_path_parts and pkg_or_mod == '__future__')
        ):  # import is already absolute
            continue

        relative_mod_path = make_pkg_subpath(*pkg_path_parts, f'{pkg_or_mod}.py')
        if relative_mod_path == file_path:  # self-import? nope! def other mod
            continue

        relative_pkg_init_path = make_pkg_subpath(*pkg_path_parts, pkg_or_mod, '__init__.py')

        possible_relative_targets = {relative_mod_path, relative_pkg_init_path}
        relative_imp_target_exists = any(
            os.path.exists(p) for p in possible_relative_targets
        )

        if not relative_imp_target_exists:
            continue

        # turn implicit relative import into an explicit absolute import
        # that is relative to the current module
        imp.value = f'.{imp.value.dumps()!s}'


def rewrite_unit_tests_patch(mod_fst, collection, spec, namespace, args):
    # FIXME duplicate code from imports rewrite
    plugins_path = ('ansible_collections', namespace, collection, 'plugins')
    tests_path = ('ansible_collections', namespace, collection, 'tests')
    unit_tests_path = tests_path + ('unit', )
    import_map = {
        ('ansible', 'modules'): plugins_path + ('modules', ),
        ('ansible', 'module_utils'): plugins_path + ('module_utils', ),
        ('ansible', 'plugins'): plugins_path,
        ('units', ): unit_tests_path,
    }

    patches = (
        mod_fst('string',
                lambda x:
                    'ansible.modules' in x.dumps() or
                    'ansible.module_utils' in x.dumps() or
                    'ansible.plugins' in x.dumps() or
                    'units' in x.dumps()
        )
    )

    deps = []
    for el in patches:
        val = el.to_python().split('.')

        for old, new in import_map.items():
            token_length = len(old)
            if tuple(val[:token_length]) != old:
                continue

            if val[0] == 'units':
                val[:token_length] = new
                el.value = "'%s'" % '.'.join(val)
                continue
            elif val[1] in ('modules', 'module_utils'):
                plugin_type = val[1]

                # 'ansible.modules.storage.netapp.na_ontap_nvme.NetAppONTAPNVMe.create_nvme'
                # look for module name
                for i in (len(val), -1, -2):
                    plugin_name = '/'.join(val[2:i])
                    try:
                        found_ns, found_coll = get_plugin_collection(plugin_name, plugin_type, spec)
                        break
                    except LookupError:
                        continue
                else:
                    continue
            elif val[1] == 'plugins':
                # 'ansible.plugins.lookup.manifold.open_url'
                try:
                    plugin_type = val[2]
                    plugin_name = val[3]
                except IndexError:
                    # Not enough information to search for the plugin, safe to assume it's not for the rewrite
                    # e.g. 'ansible.plugins.inventory'
                    continue

                try:
                    found_ns, found_coll = get_plugin_collection(plugin_name, plugin_type, spec)
                except LookupError:
                    continue
            else:
                continue

            if found_coll in COLLECTION_SKIP_REWRITE:
                continue

            if args.fail_on_core_rewrite:
                raise RuntimeError('Rewriting to %s' % '.'.join(val))

            val[:token_length] = new

            if plugin_type == 'modules' and not args.preserve_module_subdirs:
                plugin_subdirs_len = len(plugin_name.split('/')[:-1])
                new_len = len(new)
                del val[new_len:new_len+plugin_subdirs_len]

            if (found_ns, found_coll) != (namespace, collection):
                val[1] = found_ns
                val[2] = found_coll
                deps.append((found_ns, found_coll))

            el.value = "'%s'" % '.'.join(val)

    return deps


def rewrite_docs_fragments(docs, collection, spec, namespace, args):
    fragments = docs.get('extends_documentation_fragment', [])
    if not fragments:
        return [], []

    if not isinstance(fragments, list):
        fragments = [fragments]

    deps = []
    new_fragments = []
    for fragment in fragments:
        # some doc_fragments use subsections (e.g. vmware.vcenter_documentation)
        fragment_name, _dot, _rest = fragment.partition('.')
        try:
            fragment_namespace, fragment_collection = get_plugin_collection(fragment_name, 'doc_fragments', spec)
        except LookupError:
            # plugin not in spec, assuming it stays in core and leaving as is
            new_fragments.append(fragment)
            continue

        if fragment_collection in COLLECTION_SKIP_REWRITE:
            # skip rewrite
            new_fragments.append(fragment)
            continue

        if fragment_collection.startswith('_'):
            fragment_collection = fragment_collection[1:]

        new_fragment = get_plugin_fqcn(fragment_namespace, fragment_collection, fragment)
        if args.fail_on_core_rewrite:
            raise RuntimeError('Rewriting to %s' % new_fragment)

        new_fragments.append(new_fragment)

        if (namespace, collection) != (fragment_namespace, fragment_collection):
            deps.append((fragment_namespace, fragment_collection))

    return deps, new_fragments


def rewrite_plugin_documentation(mod_fst, collection, spec, namespace, args):
    try:
        doc_val = (
            mod_fst.
            find_all('assignment').
            find('name', value='DOCUMENTATION').
            parent.
            value
        )
    except AttributeError:
        raise LookupError('No DOCUMENTATION found')

    docs_parsed_dict = yaml.safe_load(doc_val.to_python().strip('\n'))
    docs_parsed_list = doc_val.to_python().split('\n')

    deps, new_fragments = rewrite_docs_fragments(docs_parsed_dict, collection, spec, namespace, args)

    # https://github.com/ansible-community/collection_migration/issues/81
    # unfortunately, with PyYAML, the resulting DOCUMENTATION ended up in syntax errors when running sanity tests
    # to prevent that, use the original string split into list for rewrites
    new_docs = []
    in_extends = False
    for line in docs_parsed_list:
        # remove version_added, it does not apply to collection in its current state
        if 'version_added' in line:
            continue

        # remove extends_documentation_fragment, it will be replaced with rewritten one below
        if 'extends_documentation_fragment' in line:
            in_extends = True
            continue
        if in_extends and '-' in line:
            continue
        else:
            in_extends = False

        new_docs.append(line)

    if new_fragments:
        new_docs.append('extends_documentation_fragment:')
        for new_fragment in new_fragments:
            new_docs.append('- %s' % new_fragment)
        new_docs.append('')

    doc_str_tmpl = RAW_STR_TMPL if doc_val.type == 'raw_string' else STR_TMPL
    # `doc_val` holds a baron representation of the string node
    # of type 'string' or 'raw_string'. Updating its `.value`
    # via assigning the new one replaces the node in FST.
    # Also, in order to generate a string or raw-string literal,
    # we need to wrap it with a corresponding pair of quotes.
    # If we don't do this, we'd generate the following Python code
    # ```
    # DOCUMENTATION = some string value
    # ```
    # instead of the correct
    # ```
    # DOCUMENTATION = r'''some string value'''
    # ```
    # or
    # ```
    # DOCUMENTATION = '''some string value'''
    # ```
    doc_val.value = doc_str_tmpl.format(str_val='\n'.join(new_docs))

    return deps


def rewrite_imports(mod_fst, collection, spec, namespace, args):
    """Rewrite imports map."""
    plugins_path = ('ansible_collections', namespace, collection, 'plugins')
    tests_path = ('ansible_collections', namespace, collection, 'tests')
    unit_tests_path = tests_path + ('unit', )
    import_map = {
        ('ansible', 'modules'): plugins_path + ('modules', ),
        ('ansible', 'module_utils'): plugins_path + ('module_utils', ),
        ('ansible', 'plugins'): plugins_path,
        ('units', ): unit_tests_path,
    }

    return rewrite_imports_in_fst(mod_fst, import_map, collection, spec, namespace, args)


def match_import_src(imp_src, import_map):
    """Find a replacement map entry matching the current import."""
    try:
        imp_src_tuple = tuple(t.value for t in imp_src)
    except AttributeError as e:
        # AttributeError("EllipsisNode instance has no attribute 'value' and 'value' is not a valid identifier of another node")
        # lib/ansible/modules/system/setup.py:
        # from ...module_utils.basic import AnsibleModule
        logger.exception(e)
        raise LookupError

    for old_imp, new_imp in import_map.items():
        token_length = len(old_imp)
        if imp_src_tuple[:token_length] != old_imp:
            continue
        return token_length, new_imp

    raise LookupError(f"Couldn't find a replacement for {imp_src!s}")


def rewrite_imports_in_fst(mod_fst, import_map, collection, spec, namespace, args):
    """Replace imports in the python module FST."""
    deps = []
    for imp in mod_fst.find_all(('import', 'from_import')):
        imp_src = imp.value
        if imp.type == 'import':
            imp_src = imp_src[0].value

        try:
            token_length, exchange = match_import_src(imp_src, import_map)
        except LookupError:
            continue  # no matching imports

        if not imp.find('name', value='g:*module_utils*') and imp.find_all('name_as_name', value='g:*Base*'):
            # from ansible.plugins.lookup import LookupBase
            # NOT 'from ansible.module_utils.azure_rm_common import AzureRMModuleBase'
            continue  # Skip imports of Base classes

        if imp.find('name_as_name', value='g:*loader*'):
            continue

        if imp.find('name_as_name', value=('AnsiblePlugin', 'PluginLoader')):
            # from ansible.plugins import AnsiblePlugin
            # from ansible.plugins.loader import PluginLoader
            continue

        if imp_src[0].value == 'units':
            imp_src[:token_length] = exchange  # replace the import
            continue
        elif imp_src[1].value == 'module_utils':
            plugin_type = 'module_utils'
            try:
                plugin_name = '/'.join([t.value for t in imp_src[token_length:]] + [imp.targets[0].value])
            except AttributeError:
                plugin_name = '/'.join(t.value for t in imp_src[token_length:])

            if not plugin_name:
                # 'from ansible.module_utils import distro'
                plugin_name = imp.targets[0].value
                # NOTE multiple targets? - git grep says there is not such case now
                # NOTE 'from ansible.module_utils import $module as $alias'? - git grep says there is not such case now
        elif imp_src[1].value == 'plugins':
            try:
                plugin_type = imp_src[2].value
                plugin_name = imp_src[3].value
            except IndexError:
                if len(getattr(imp, 'targets', [])) == 1:
                    # from ansible.plugins.connection import winrm
                    plugin_name = imp.targets[0].value
                else:
                    logger.error('Could not get plugin type or name from ' + str(imp) + '. Is this expected?')
                    continue
        elif imp_src[1].value == 'modules':
            # in unit tests
            plugin_type = 'modules'
            try:
                # from ansible.modules.network.nxos import nxos_bgp
                plugin_name = '/'.join([t.value for t in imp_src[token_length:]] + [imp.targets[0].value])
            except AttributeError:
                # import ansible.modules.cloud.amazon.aws_api_gateway as agw
                plugin_name = '/'.join(t.value for t in imp_src[token_length:])
        else:
            raise Exception('BUG: Could not process import: ' + str(imp))

        try:
            plugin_namespace, plugin_collection = get_plugin_collection(plugin_name, plugin_type, spec)
        except LookupError:
            if plugin_type not in ('modules', 'module_utils'):
                # plugin not in spec, assuming it stays in core and skipping
                continue

            # from ansible.modules.cloud.amazon.aws_netapp_cvs_FileSystems import AwsCvsNetappFileSystem as fileSystem_module
            # in this case aws_netapp_cvs_FileSystems is the module, not AwsCvsNetappFileSystem
            try:
                plugin_name = '/'.join(plugin_name.split('/')[:-1])
                plugin_namespace, plugin_collection = get_plugin_collection(plugin_name, plugin_type, spec)
            except LookupError:
                # plugin not in spec, assuming it stays in core and skipping
                continue

        if plugin_collection in COLLECTION_SKIP_REWRITE:
            # skip rewrite
            continue

        if args.fail_on_core_rewrite:
            raise RuntimeError('Rewriting to %s.%s.%s' % (plugin_namespace, plugin_collection, plugin_name))

        if plugin_collection.startswith('_'):
            plugin_collection = plugin_collection[1:]

        imp_src[:token_length] = exchange  # replace the import

        if plugin_type == 'modules' and not args.preserve_module_subdirs:
            plugin_subdirs_len = len(plugin_name.split('/')[:-1])
            exchange_len = len(exchange)
            del imp_src[exchange_len:exchange_len+plugin_subdirs_len]

        if (plugin_namespace, plugin_collection) != (namespace, collection):
            imp_src[1] = plugin_namespace
            imp_src[2] = plugin_collection
            deps.append((plugin_namespace, plugin_collection))

    return deps


def rewrite_py(src, dest, collection, spec, namespace, args):
    with fst_rewrite_session(src, dest) as mod_fst:
        import_deps = rewrite_imports(mod_fst, collection, spec, namespace, args)

        try:
            docs_deps = rewrite_plugin_documentation(mod_fst, collection, spec, namespace, args)
        except LookupError as err:
            docs_deps = []
            logger.debug('%s in %s', err, src)

        rewrite_class_property(mod_fst, collection, namespace, dest)

    return (import_deps, docs_deps)


@contextlib.contextmanager
def fst_rewrite_session(src_path, dst_path):
    """Parse the module FST and save it to disk afterwards."""
    mod_src_text, mod_fst = read_module_txt_n_fst(src_path)

    yield mod_fst

    new_mod_src_text = mod_fst.dumps()

    if src_path == dst_path and mod_src_text == new_mod_src_text:
        return

    logger.info('Rewriting plugin references in %s', dst_path)
    write_text_into_file(dst_path, new_mod_src_text)


def read_module_txt_n_fst(path):
    """Parse module source code in form of Full Syntax Tree."""
    mod_src_text = read_text_from_file(path)
    try:
        return mod_src_text, redbaron.RedBaron(mod_src_text)
    except ParsingError:
        logger.exception('failed parsing on %s', mod_src_text)
        raise


def inject_init_into_tree(target_dir):
    """Insert ``__init__.py`` file into unit test directories."""
    for root, dirs, files in os.walk(target_dir):
        python_files_exist = any(fn.endswith('.py') for fn in files)
        possibly_subpackages_exist = len(dirs) > 0
        if '__init__.py' not in files and (python_files_exist or possibly_subpackages_exist):
            write_text_into_file(os.path.join(root, '__init__.py'), '')


def inject_readme_into_collection(collection_dir, *, ctx):
    """Insert a ``README.md`` file into the collection dir.

    The ``README.md.tmpl`` resource template file contains a title
    and a GitHub Actions Workflow badge.
    """
    target_file = 'README.md'
    render_template_into(f'{target_file}.tmpl', ctx, os.path.join(collection_dir, target_file))


def inject_github_actions_workflow_into_collection(collection_dir, *, ctx):
    """Insert GitHub Actions Workflow config into collection repo."""
    target_file = 'collection-continuous-integration.yml'

    workflows_dir = os.path.join(collection_dir, '.github', 'workflows')
    os.makedirs(workflows_dir, exist_ok=True)

    render_template_into(f'{target_file}.tmpl', ctx, os.path.join(workflows_dir, target_file))


def inject_gitignore_into_collection(collection_dir):
    """Insert a ``.gitignore`` file into the collection dir.

    The ``.gitignore`` resource file has been generated
    using the following command:

      curl -sL https://www.gitignore.io/api/git%2Clinux%2Cpydev%2Cpython%2Cwindows%2Cpycharm%2Ball%2Cjupyternotebook%2Cvim%2Cwebstorm%2Cemacs%2Cdotenv > resources/.gitignore.tmpl
    """
    gitignore_resource_path = os.path.join(os.path.dirname(__file__), 'resources', '.gitignore.tmpl')
    shutil.copy(gitignore_resource_path, os.path.join(collection_dir, '.gitignore'))


def inject_gitignore_into_tests(collection_dir):
    """Generate a ``.gitignore`` file for the collection tests dir."""
    os.makedirs(os.path.join(collection_dir, 'tests'), exist_ok=True)
    write_text_into_file(
        os.path.join(collection_dir, 'tests', '.gitignore'),
        textwrap.dedent('''
        output/
        ''').lstrip('\n'),
    )


def generate_converted_ignore_contents(original_ignore_contents, file_map):
    """Emit lines for the converted sanity test ignore file."""
    for line in original_ignore_contents.splitlines():
        file_path, sep, ignored_rules = line.partition(' ')
        with contextlib.suppress(KeyError):
            yield sep.join((file_map[file_path], ignored_rules))


def inject_ignore_into_sanity_tests(checkout_path, collection_dir, migrated_files_map):
    """Inject sanity test ignore lists into collection sanity tests."""
    coll_sanity_tests_dir = os.path.join(collection_dir, 'tests', 'sanity')
    original_ignore_contents = read_text_from_file(os.path.join(checkout_path, 'test', 'sanity', 'ignore.txt'))
    converted_ignore_contents = '\n'.join(
        generate_converted_ignore_contents(
            original_ignore_contents,
            migrated_files_map,
        ),
    )

    if not converted_ignore_contents:
        return

    os.makedirs(coll_sanity_tests_dir, exist_ok=True)
    # latest stable
    write_text_into_file(os.path.join(coll_sanity_tests_dir, 'ignore-2.9.txt'), converted_ignore_contents)
    # devel/future release
    write_text_into_file(os.path.join(coll_sanity_tests_dir, 'ignore-2.10.txt'), converted_ignore_contents)


def inject_requirements_into_unit_tests(checkout_path, collection_dir):
    """Inject unit tests dependencies into collection."""
    coll_unit_tests_dir = os.path.join(collection_dir, 'tests', 'unit')
    original_unit_tests_req_file = os.path.join(checkout_path, 'test', 'units', 'requirements.txt')

    os.makedirs(coll_unit_tests_dir, exist_ok=True)
    shutil.copy(original_unit_tests_req_file, coll_unit_tests_dir)

    logger.info('Unit tests deps injected into collection')


def inject_requirements_into_sanity_tests(checkout_path, collection_dir):
    """Inject sanity tests Python dependencies into collection."""
    coll_sanity_tests_dir = os.path.join(collection_dir, 'tests', 'sanity',)
    original_sanity_tests_req_file = os.path.join(checkout_path, 'test', 'sanity', 'requirements.txt')

    os.makedirs(coll_sanity_tests_dir, exist_ok=True)
    shutil.copy(original_sanity_tests_req_file, coll_sanity_tests_dir)
    logger.info('Sanity tests deps injected into collection')


def create_unit_tests_copy_map(checkout_path, plugin_type, plugin):
    """Find all unit tests and related artifacts for the given plugin.

    Return the copy map.
    """
    type_subdir = (
        plugin_type
        if plugin_type in ('modules', 'module_utils')
        else os.path.join('plugins', plugin_type)
    )

    unit_tests_relative_root = os.path.join('test', 'units')

    collection_unit_tests_relative_root = os.path.join('tests', 'unit')

    # Narrow down the search area
    type_base_subdir = os.path.join(
        checkout_path, unit_tests_relative_root, type_subdir,
    )

    # Figure out what to copy and where
    copy_map = {}

    # Find all test modules with the same ending as the current plugin
    plugin_dir, plugin_mod = os.path.split(plugin)
    matching_test_modules = set(glob.glob(os.path.join(
        type_base_subdir,
        plugin_dir,
        f'*{plugin_mod}',
    )))
    # plugin_mod might also be a directory, scan subdirs
    plugin_mod = os.path.splitext(plugin_mod)[0]
    matching_test_modules = matching_test_modules.union(set(glob.glob(os.path.join(
        type_base_subdir,
        plugin_dir,
        f'*{plugin_mod}/**',
    ), recursive=True)))
    matching_test_modules = set(f for f in matching_test_modules if not os.path.isdir(f))

    # Path(matching_test_modules[0]).relative_to(Path(checkout_path))
    # os.path.relpath(matching_test_modules[0], checkout_path)
    if not matching_test_modules:
        logger.info('No unit tests matching %s/%s found', plugin_type, plugin)
        return copy_map

    def find_up_the_tree(target_path):
        """Find conftest.py in the parent test dirs."""
        needle_filename = 'conftest.py'
        tests_root = os.path.join(checkout_path, unit_tests_relative_root)

        relative_target_path = os.path.relpath(target_path, tests_root)
        if relative_target_path.startswith('..'):
            raise ValueError(
                f'`{target_path}` is not a part of `{tests_root}`',
            )

        logger.info(
            'Locating parent %s '
            'for %s...',
            needle_filename,
            target_path,
        )
        while relative_target_path:
            relative_target_path, _ = os.path.split(relative_target_path)

            target_file = os.path.join(
                tests_root,
                relative_target_path,
                needle_filename,
            )
            if not os.path.isfile(target_file):
                continue

            logger.info(
                'Located %s...',
                target_file
            )
            yield target_file

    # Discover constest.py's from parent dirs:
    conftest_modules = set(
        p
        for m in matching_test_modules.copy()
        for p in find_up_the_tree(m)
    )

    def traverse_dir(path, relative_to):
        rel_path = os.path.join(relative_to, path)
        if not os.path.isdir(rel_path):
            return {path}

        matching_files = itertools.chain(
            glob.iglob(
                os.path.join(rel_path, '.**'),
                recursive=True,
            ),
            glob.iglob(
                os.path.join(rel_path, '**'),
                recursive=True,
            ),
        )
        return set(
            os.path.relpath(p, relative_to)
            for p in matching_files
            if not os.path.isdir(p)
        )

    def replace_path_prefix(path):
        return os.path.join(
            collection_unit_tests_relative_root,
            os.path.relpath(path, unit_tests_relative_root),
        )

    # Inject unit test helper packages
    # TODO: figure out the bug with path maps
    compat_mock_helpers = (
        (
            UnmovablePathStr(src_f),
            replace_path_prefix(src_f),
        )
        for hd in {'compat', 'mock', 'modules/utils.py'}
        for src_f in traverse_dir(
            os.path.join(unit_tests_relative_root, hd),
            checkout_path,
        )
    )
    copy_map.update(compat_mock_helpers)

    def discover_file_migrations(paths, *, find_related=False):
        """Generate the migration map for given paths.

        Optionally, traverse siblings.
        """
        nonlocal unit_tests_relative_root

        for td, tm in map(os.path.split, paths):
            relative_td = os.path.relpath(td, checkout_path)
            test_artifact_path = os.path.join(relative_td, tm)
            yield (
                test_artifact_path,
                replace_path_prefix(test_artifact_path),
            )

            if not find_related:
                continue

            module_type = os.path.relpath(
                relative_td,
                unit_tests_relative_root,
            )

            if module_type in {'module_utils'}:
                """Top-level dir of the module_utils unit tests."""
                continue

            # Add subdirs that may contain related test artifacts/fixtures
            # Also add important modules like conftest or __init__
            related_test_fixtures = itertools.chain.from_iterable(
                traverse_dir(
                    os.path.join(relative_td, path),
                    checkout_path,
                )
                for path in os.listdir(td)
                if os.path.isdir(os.path.join(td, path))
                or not path.startswith('test_')
            )

            yield from (
                (
                    test_artifact_path,
                    replace_path_prefix(test_artifact_path),
                )
                for test_artifact_path in related_test_fixtures
            )

    copy_map.update(itertools.chain(
        discover_file_migrations(conftest_modules),
        discover_file_migrations(matching_test_modules, find_related=True),
    ))

    return copy_map


def copy_unit_tests(copy_map, collection_dir, checkout_path):
    """Copy unit tests into a collection using a copy map."""
    if not copy_map:
        logger.info(
            'No unit tests scheduled for copying to %s', collection_dir,
        )
        return

    for src_f, dest_f in copy_map.items():
        if os.path.splitext(src_f)[1] in BAD_EXT:
            continue

        should_be_preserved = isinstance(src_f, UnmovablePathStr)

        dest = os.path.join(collection_dir, dest_f)
        # Ensure target dir exists
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        src = os.path.join(checkout_path, src_f)
        logger.info('Migrating %s -> %s', src, dest)
        shutil.copy(src, dest)

        if should_be_preserved:
            continue

        remove(src)

    inject_requirements_into_unit_tests(checkout_path, collection_dir)


### MAKE COLLECTIONS

def assemble_collections(checkout_path, spec, args, target_github_org):
    collections_base_dir = os.path.join(args.vardir, 'collections')

    # expand globs so we deal with specific paths
    resolve_spec(spec, checkout_path)

    # ensure we always use a clean copy
    if args.refresh and os.path.exists(collections_base_dir):
        shutil.rmtree(collections_base_dir)

    # make initial YAML transformation to minimize the diff
    mark_moved_resources(checkout_path, 'N/A', 'init', {})

    seen = {}
    for namespace in spec.keys():
        for collection in spec[namespace].keys():
            import_deps = []
            docs_deps = []
            unit_deps = []
            integration_test_dirs = []
            migrated_to_collection = {}
            unit_tests_copy_map = {}

            if args.fail_on_core_rewrite:
                if collection != '_core':
                    continue
            else:
                if collection.startswith('_'):
                    # these are info only collections
                    continue

            collection_dir = os.path.join(collections_base_dir, 'ansible_collections', namespace, collection)

            if args.refresh and os.path.exists(collection_dir):
                shutil.rmtree(collection_dir)

            if not os.path.exists(collection_dir):
                os.makedirs(collection_dir)

            # create the data for galaxy.yml
            galaxy_metadata = init_galaxy_metadata(collection, namespace, target_github_org)

            # process each plugin type
            for plugin_type, plugins in spec[namespace][collection].items():
                if not plugins:
                    logger.error('Empty plugin_type: %s in spec for %s.%s', plugin_type, namespace, collection)
                    continue

                # get src plugin path
                src_plugin_base = PLUGIN_EXCEPTION_PATHS.get(plugin_type, os.path.join('lib', 'ansible', 'plugins', plugin_type))

                # ensure destinations exist
                if plugin_type in PLUGIN_DEST_EXCEPTION_PATHS:
                    relative_dest_plugin_base = PLUGIN_DEST_EXCEPTION_PATHS[plugin_type]
                else:
                    relative_dest_plugin_base = os.path.join('plugins', plugin_type)
                dest_plugin_base = os.path.join(collection_dir, relative_dest_plugin_base)
                if not os.path.exists(dest_plugin_base):
                    os.makedirs(dest_plugin_base)
                    write_text_into_file(os.path.join(dest_plugin_base, '__init__.py'), '')

                # process each plugin
                for plugin in plugins:
                    if os.path.splitext(plugin)[1] in BAD_EXT:
                        raise Exception("We should not be migrating compiled files: %s" % plugin)

                    plugin_sig = '%s/%s' % (plugin_type, plugin)
                    if plugin_sig in seen:
                        raise ValueError(
                            'Each plugin needs to be assigned to one collection '
                            f'only. {plugin_sig} has already been processed as a '
                            f'part of `{seen[plugin_sig]}` collection.'
                        )
                    seen[plugin_sig] = collection

                    # TODO: currently requires 'full name of file', but should work w/o extension?
                    relative_src_plugin_path = os.path.join(src_plugin_base, plugin)
                    src = os.path.join(checkout_path, relative_src_plugin_path)

                    # TODO: collections are now scheduled to handle deprecations and aliases, until we get an implementation
                    # we are just treating them as normal files for now (previously we were avoiding them).

                    #if os.path.basename(plugin).startswith('_') and os.path.basename(plugin) != '__init__.py':
                    #    if os.path.islink(src):
                    #        logger.info("Removing plugin alias from checkout and skipping: %s (%s in %s.%s)",
                    #                     plugin, plugin_type, namespace, collection)
                    #        remove(src)
                    #    else:
                    #        logger.error("We should not be migrating deprecated plugins, skipping: %s (%s in %s.%s)",
                    #                     plugin, plugin_type, namespace, collection)
                    #    continue

                    remove(src)

                    if plugin_type in ('modules',) and '/' in plugin:
                        init_py_path = os.path.join(os.path.dirname(src), '__init__.py')
                        if os.path.exists(init_py_path):
                            remove(init_py_path)

                    do_preserve_subdirs = (
                        (args.preserve_module_subdirs and plugin_type == 'modules')
                        or plugin_type == 'module_utils'
                    )
                    plugin_path_chunk = plugin if do_preserve_subdirs else os.path.basename(plugin)
                    relative_dest_plugin_path = os.path.join(relative_dest_plugin_base, plugin_path_chunk)

                    migrated_to_collection[relative_src_plugin_path] = relative_dest_plugin_path

                    dest = os.path.join(collection_dir, relative_dest_plugin_path)
                    if do_preserve_subdirs:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)

                    if not os.path.exists(src):
                        raise Exception('Spec specifies "%s" but file "%s" is not found in checkout' % (plugin, src))

                    if os.path.islink(src):
                        process_symlink(plugin_type, plugins, dest, src)
                        # don't rewrite symlinks, original file should already be handled
                        continue
                    elif not src.endswith('.py'):
                        # its not all python files, copy and go to next
                        # TODO: handle powershell import rewrites
                        shutil.copyfile(src, dest)
                        continue

                    logger.info('Processing %s -> %s', src, dest)

                    deps = rewrite_py(src, dest, collection, spec, namespace, args)
                    import_deps += deps[0]
                    docs_deps += deps[1]

                    if args.skip_tests or plugin_type in NOT_PLUGINS:
                        # skip rest for 'not really plugins'
                        continue

                    integration_test_dirs.extend(poor_mans_integration_tests_discovery(checkout_path, plugin_type, plugin))

                    # process unit tests
                    plugin_unit_tests_copy_map = create_unit_tests_copy_map(
                        checkout_path, plugin_type, plugin,
                    )
                    unit_tests_copy_map.update(plugin_unit_tests_copy_map)

            if not args.skip_tests:
                copy_unit_tests(unit_tests_copy_map, collection_dir, checkout_path)
                migrated_to_collection.update(unit_tests_copy_map)

                inject_init_into_tree(
                    os.path.join(collection_dir, 'tests', 'unit'),
                )

                unit_deps += rewrite_unit_tests(collection_dir, collection, spec, namespace, args)

                inject_gitignore_into_tests(collection_dir)

                inject_ignore_into_sanity_tests(
                    checkout_path, collection_dir, migrated_to_collection,
                )
                inject_requirements_into_sanity_tests(checkout_path, collection_dir)

                # FIXME need to hack PyYAML to preserve formatting (not how much it's possible or how much it is work) or use e.g. ruamel.yaml
                try:
                    migrated_integration_test_files = rewrite_integration_tests(integration_test_dirs, checkout_path, collection_dir, namespace, collection, spec, args)
                    migrated_to_collection.update(migrated_integration_test_files)
                except yaml.composer.ComposerError as e:
                    logger.error(e)

                global integration_tests_deps
                add_deps_to_metadata(integration_tests_deps.union(import_deps + docs_deps + unit_deps), galaxy_metadata)
                integration_tests_deps = set()

            inject_gitignore_into_collection(collection_dir)
            j2_ctx = {
                'coll_ns': namespace,
                'coll_name': collection,
                'gh_org': target_github_org,
            }
            inject_readme_into_collection(
                collection_dir,
                ctx=j2_ctx,
            )
            inject_github_actions_workflow_into_collection(
                collection_dir,
                ctx=j2_ctx,
            )

            # write collection metadata
            write_yaml_into_file_as_is(
                os.path.join(collection_dir, 'galaxy.yml'),
                galaxy_metadata,
            )

            # init git repo
            subprocess.check_call(('git', 'init'), cwd=collection_dir)
            subprocess.check_call(('git', 'add', '.'), cwd=collection_dir)
            subprocess.check_call(
                ('git', 'commit', '-m', 'Initial commit', '--allow-empty'),
                cwd=collection_dir,
            )

            mark_moved_resources(
                checkout_path, namespace, collection, migrated_to_collection,
            )

            if args.move_plugins:
                actually_remove(checkout_path, namespace, collection)

            global REMOVE
            REMOVE = set()


def init_galaxy_metadata(collection, namespace, target_github_org):
    """Return the initial Galaxy collection metadata object."""
    github_repo_slug = f'{target_github_org}/{namespace}.{collection}'
    github_repo_http_url = f'https://github.com/{github_repo_slug}'
    github_repo_ssh_url = f'git@github.com:{github_repo_slug}.git'
    return {
        'namespace': namespace,
        'name': collection,
        'version': '1.0.0',  # TODO: add to spec, args?
        'readme': 'README.md',
        'authors': None,
        'description': None,
        'license': None,
        'license_file': None,
        'tags': None,
        'dependencies': {},
        'repository': github_repo_ssh_url,
        'documentation': f'{github_repo_http_url}/tree/master/docs',
        'homepage': github_repo_http_url,
        'issues': (
            f'{github_repo_http_url}/issues'
            '?q=is%3Aissue+is%3Aopen+sort%3Aupdated-desc'
        ),
    }


def process_symlink(plugin_type, plugins, dest, src):
    """Recreate plugin module symlinks."""
    real_src = os.readlink(src)

    # remove destination if it already exists
    if os.path.exists(dest):
        # NOTE: not atomic but should not matter in our script
        logger.warning('Removed "%s" as it is target for symlink of "%s"', dest, src)
        os.remove(dest)

    if not real_src.startswith('../'):
        shutil.copyfile(src, dest, follow_symlinks=False)
        return

    target = real_src[3:]
    found = any(p.endswith(target) for p in plugins)

    if not found:
        raise LookupError('Found symlink "%s" to target "%s" that is not in same collection.' % (src, target))

    if plugin_type == 'module_utils':
        target = real_src
    else:
        target = os.path.basename(real_src)

    with working_directory(os.path.dirname(dest)):
        os.symlink(os.path.basename(target), os.path.basename(dest))


def rewrite_unit_tests(collection_dir, collection, spec, namespace, args):
    """Rewrite imports and apply patches to unit tests."""
    deps = []

    for file_path in itertools.chain.from_iterable(
            (os.path.join(dp, f) for f in fn if f.endswith('.py'))
            for dp, dn, fn in os.walk(os.path.join(collection_dir, 'tests', 'unit'))
    ):
        with fst_rewrite_session(file_path, file_path) as unit_test_module_fst:
            deps += rewrite_imports(
                unit_test_module_fst, collection, spec, namespace, args,
            )
            deps += rewrite_unit_tests_patch(
                unit_test_module_fst, collection, spec, namespace, args,
            )
            normalize_implicit_relative_imports_in_unit_tests(
                unit_test_module_fst, file_path,
            )

    return deps


def add_deps_to_metadata(deps, galaxy_metadata):
    """Augment galaxy metadata with the dependencies."""
    for dep_ns, dep_coll in deps:
        dep = '%s.%s' % (dep_ns, dep_coll)
        # FIXME hardcoded version
        galaxy_metadata['dependencies'][dep] = '>=1.0'


def publish_to_github(collections_target_dir, spec, github_api, rsa_key):
    """Push all migrated collections to their Git remotes."""
    collections_base_dir = os.path.join(collections_target_dir, 'collections')
    collections_root_dir = os.path.join(
        collections_base_dir,
        'ansible_collections',
    )
    collection_paths_except_core = (
        (os.path.join(collections_root_dir, ns, coll), f'{ns}.{coll}')
        for ns, ns_val in spec.items()
        for coll in ns_val.keys()
        if not coll.startswith('_')
    )
    logger.debug('Using SSH key %s...', rsa_key.public_openssh)
    with rsa_key.ssh_agent as ssh_agent:
        for collection_dir, repo_name in collection_paths_except_core:
            galaxy_yml = read_yaml_file(
                os.path.join(collection_dir, 'galaxy.yml'),
            )
            git_repo_url = galaxy_yml['repository']
            coll_home_url = galaxy_yml['repository']
            git_repo_url_repr = '...'.join((
                git_repo_url[:5], git_repo_url[-5:],
            )) if not git_repo_url.startswith('git@') else git_repo_url
            logger.info(
                'Forcefully pushing the migrated collection `%s` '
                'to GitHub org `%s` using `%s` Git URL for push',
                repo_name,
                github_api.github_org_name,
                git_repo_url_repr,
            )
            git_force_push_cmd = (
                'git', 'push', '--force', git_repo_url, 'HEAD:master',
            )
            with github_api.tmp_deployment_key_for(repo_name):
                ensure_cmd_succeeded(
                    ssh_agent, git_force_push_cmd, collection_dir,
                )
            logger.info(
                'The migrated collection has been successfully published to '
                '`%s` GitHub repository...',
                coll_home_url,
            )


def push_migrated_core(releases_dir, github_api, rsa_key, spec_dir):
    devel_path = os.path.join(releases_dir, f'{DEVEL_BRANCH}.git')

    scenario_name = os.path.basename(spec_dir.rstrip('/'))
    migrated_devel_repo_name = f'ansible-{scenario_name}'
    migrated_devel_remote = (
        f'git@github.com:{github_api.github_org_name}/'
        f'{migrated_devel_repo_name}.git'
    )
    git_force_push_cmd = (
        'git', 'push', '--force', migrated_devel_remote, DEVEL_BRANCH,
    )

    logger.debug('Using SSH key %s...', rsa_key.public_openssh)
    with rsa_key.ssh_agent as ssh_agent, github_api.tmp_deployment_key_for(
            migrated_devel_repo_name,
    ):
        # NOTE: assumes the repo is not used and/or is locked while migration is running
        ensure_cmd_succeeded(ssh_agent, git_force_push_cmd, devel_path)


def assert_migrating_git_tracked_resources(
        migrated_to_collection: Union[Iterable[str], Dict[str, Any]],
):
    """Make sure that non-tracked files aren't scheduled for migration.

    :param migrated_to_collection: Iterable of paths relative \
                                   to the ansible/ansible root.
    """
    logger.info(
        'Verifying that only legitimate files are being migrated...',
    )
    for migrated_resource in migrated_to_collection:
        exists_in_src = migrated_resource in ALL_THE_FILES
        if not exists_in_src:
            err_msg = (
                f'{migrated_resource} does not '
                'exist in the source'
            )
            logger.error(err_msg)
            raise RuntimeError(err_msg)


def mark_moved_resources(checkout_dir, namespace, collection, migrated_to_collection):
    """Mark migrated paths in botmeta."""
    migrated_to_collection = {str(k): str(v) for k, v in migrated_to_collection.items()}
    assert_migrating_git_tracked_resources(migrated_to_collection)

    migrated_to = '.'.join((namespace, collection))
    botmeta_rel_path = '.github/BOTMETA.yml'
    botmeta_checkout_path = os.path.join(checkout_dir, botmeta_rel_path)

    botmeta = read_yaml_file(botmeta_checkout_path)

    botmeta_files = botmeta['files']
    botmeta_file_paths = botmeta_files.keys()
    botmeta_macros = botmeta['macros']

    transformed_path_key_map = {}
    for k in botmeta_file_paths:
        transformed_key = Template(k).substitute(**botmeta_macros)
        if transformed_key == k:
            continue
        transformed_path_key_map[transformed_key] = k

    for migrated_resource in migrated_to_collection:
        macro_path = transformed_path_key_map.get(
            migrated_resource, migrated_resource,
        )

        migrated_secion = botmeta_files.get(macro_path)
        if not migrated_secion:
            migrated_secion = botmeta_files[macro_path] = {}
        elif isinstance(migrated_secion, str):
            migrated_secion = botmeta_files[macro_path] = {
                'maintainers': migrated_secion,
            }

        migrated_secion['migrated_to'] = migrated_to

    write_yaml_into_file_as_is(botmeta_checkout_path, botmeta)

    # Commit changes to the migrated Git repo
    subprocess.check_call(
        ('git', 'add', f'{botmeta_rel_path!s}'),
        cwd=checkout_dir,
    )
    subprocess.check_call(
        (
            'git', 'commit',
            '-m', f'Mark migrated {collection}',
            '--allow-empty',
        ),
        cwd=checkout_dir,
    )


### Rewrite integration tests

integration_tests_deps = set()

def integration_tests_add_to_deps(collection, dep_collection):
    if collection == dep_collection:
        return

    global integration_tests_deps

    if dep_collection not in integration_tests_deps:
        logger.info("Adding %s.%s as a dep for %s.%s", dep_collection[0], dep_collection[1], collection[0], collection[1])

    integration_tests_deps.add(dep_collection)


def poor_mans_integration_tests_discovery(checkout_dir, plugin_type, plugin_name):
    # FIXME this might be actually enough for modules integration tests, at least for the most part
    if plugin_type != 'modules':
        return []

    files = [
        (file_, True) for file_ in glob.glob(os.path.join(checkout_dir, 'test/integration/targets', os.path.basename(os.path.splitext(plugin_name)[0])))
    ]
    deps = []
    for fname, dummy_to_remove in files:
        logger.info('Found integration tests for %s %s in %s', plugin_type, plugin_name, fname)
        deps.extend(process_needs_target(checkout_dir, fname))

    return files + deps


def process_needs_target(checkout_dir, fname):
    deps = []
    dep_file = os.path.join(fname, 'meta', 'main.yml')
    if os.path.exists(dep_file):
        content = read_yaml_file(dep_file)
        if content:
            meta_deps = content.get('dependencies', {}) or {}
            for dep in meta_deps:
                if isinstance(dep, dict):
                    dep = dep.get('role')
                dep_fname = os.path.join(checkout_dir, 'test/integration/targets', dep)
                logger.info('Adding integration tests dependency target %s for %s', dep_fname, fname)
                deps.append((dep_fname, False))
                deps.extend(process_needs_target(checkout_dir, dep_fname))

    aliases_file = os.path.join(fname, 'aliases')
    if os.path.exists(aliases_file):
        content = read_text_from_file(aliases_file)
        for alias in content.split('\n'):
            if not alias.startswith('needs/target/'):
                continue
            dep = alias.split('/')[-1]
            dep_fname = os.path.join(checkout_dir, 'test/integration/targets', dep)
            if os.path.exists(dep_fname):
                logger.info('Adding integration tests dependency target %s for %s', dep_fname, fname)
                deps.append((dep_fname, False))
                deps.extend(process_needs_target(checkout_dir, dep_fname))

    return deps


def rewrite_integration_tests(test_dirs, checkout_dir, collection_dir, namespace, collection, spec, args):
    # FIXME module_defaults groups
    logger.info('Processing integration tests for %s.%s', namespace, collection)

    migrated = {}
    for test_dir, to_remove in test_dirs:
        for dirpath, dirnames, filenames in os.walk(test_dir):
            for filename in filenames:
                src = os.path.join(dirpath, filename)

                dest_dir = os.path.join(collection_dir,
                                        'tests',
                                        os.path.relpath(dirpath, os.path.join(checkout_dir, 'test')))
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
                dest = os.path.join(dest_dir, filename)

                dummy, ext = os.path.splitext(filename)

                logger.info('Processing %s -> %s', src, dest)

                if ext in BAD_EXT:
                    continue
                elif ext in ('.py',):
                    import_deps, docs_deps = rewrite_py(src, dest, collection, spec, namespace, args)

                    for dep_ns, dep_coll in import_deps + docs_deps:
                        integration_tests_add_to_deps((namespace, collection), (dep_ns, dep_coll))
                elif ext in ('.ps1',):
                    # FIXME
                    shutil.copy2(src, dest)
                elif ext in ('.yml', '.yaml'):
                    rewrite_yaml(src, dest, namespace, collection, spec, args)
                elif ext in ('.sh',):
                    rewrite_sh(src, dest, namespace, collection, spec, args)
                elif filename == 'ansible.cfg':
                    rewrite_ini(src, dest, namespace, collection, spec, args)
                else:
                    shutil.copy2(src, dest)

                if to_remove:
                    remove(src)

                relative_src_path = os.path.relpath(src, checkout_dir)
                relative_dest_path = os.path.relpath(dest, collection_dir)
                migrated[relative_src_path] = relative_dest_path

    return migrated


def rewrite_sh(src, dest, namespace, collection, spec, args):
    sh_key_map = {
        'ANSIBLE_CACHE_PLUGIN': 'cache',
        'ANSIBLE_CALLBACK_WHITELIST': 'callback',
        'ANSIBLE_INVENTORY_CACHE_PLUGIN': 'cache',
        'ANSIBLE_STDOUT_CALLBACK': 'callback',
        'ANSIBLE_STRATEGY': 'strategy',
        '--become-method': 'become',
        '-c': 'connection',
        '--connection': 'connection',
    }

    contents = read_text_from_file(src)
    for key, plugin_type in sh_key_map.items():
        if contents.find(key) == -1:
            continue
        for ns in spec.keys():
            for coll in get_rewritable_collections(ns, spec):
                plugins = get_plugins_from_collection(ns, coll, plugin_type, spec)
                for plugin_name in plugins:
                    if contents.find(plugin_name) == -1:
                        continue
                    # FIXME list
                    new_plugin_name = get_plugin_fqcn(ns, coll, plugin_name)
                    msg = 'Rewriting to %s' % new_plugin_name
                    if args.fail_on_core_rewrite:
                        raise RuntimeError(msg)

                    logger.debug(msg)
                    contents = contents.replace(key + '=' + plugin_name, key + '=' + new_plugin_name)
                    contents = contents.replace(key + ' ' + plugin_name, key + ' ' + new_plugin_name)
                    integration_tests_add_to_deps((namespace, collection), (ns, coll))

    write_text_into_file(dest, contents)
    shutil.copystat(src, dest)


def rewrite_ini(src, dest, namespace, collection, spec, args):
    ini_key_map = {
        'defaults': {
            'callback_whitelist': 'callback',
            'fact_caching': 'cache',
            'strategy': 'strategy',
            'stdout_callback': 'callback',
        },
        'inventory': {
            'cache_plugin': 'cache',
            'enable_plugins': 'inventory',
        }
    }

    config = configparser.ConfigParser()
    config.read(src)
    for section in config.sections():
        try:
            rewrite_ini_section(config, ini_key_map, section, namespace, collection, spec, args)
        except KeyError:
            continue

    with open(dest, 'w') as cf:
        config.write(cf)


def rewrite_ini_section(config, key_map, section, namespace, collection, spec, args):
    for keyword, plugin_type in key_map[section].items():
        try:
            # FIXME diff input format than csv?
            plugin_names = config.get(section, keyword).split(',')
        except configparser.NoOptionError:
            continue

        new_plugin_names = []
        for plugin_name in plugin_names:
            try:
                plugin_namespace, plugin_collection = get_plugin_collection(plugin_name, plugin_type, spec)
                if plugin_collection in COLLECTION_SKIP_REWRITE:
                    raise LookupError

                msg = 'Rewriting to %s.%s.%s' % (plugin_namespace, plugin_collection, plugin_name)
                if args.fail_on_core_rewrite:
                    raise RuntimeError(msg)

                logger.debug(msg)
                new_plugin_names.append(get_plugin_fqcn(plugin_namespace, plugin_collection, plugin_name))
                integration_tests_add_to_deps((namespace, collection), (plugin_namespace, plugin_collection))
            except LookupError:
                new_plugin_names.append(plugin_name)

        config.set(section, keyword, ','.join(new_plugin_names))


def rewrite_yaml(src, dest, namespace, collection, spec, args):
    try:
        contents = read_ansible_yaml_file(src)
        _rewrite_yaml(contents, namespace, collection, spec, args, dest)
        write_ansible_yaml_into_file_as_is(dest, contents)
    except Exception as e:
        logger.error('Skipping bad YAML in %s: %s', src, str(e))


def _rewrite_yaml(contents, namespace, collection, spec, args, dest):
    if isinstance(contents, list):
        for el in contents:
            _rewrite_yaml(el, namespace, collection, spec, args, dest)
    elif isinstance(contents, Mapping):
        _rewrite_yaml_mapping(contents, namespace, collection, spec, args, dest)


def _rewrite_yaml_mapping(el, namespace, collection, spec, args, dest):
    assert isinstance(el, Mapping)

    _rewrite_yaml_mapping_keys_vars(el, namespace, collection, spec, args, dest)
    _rewrite_yaml_mapping_keys_non_vars(el, namespace, collection, spec, args, dest)
    _rewrite_yaml_mapping_values(el, namespace, collection, spec, args, dest)


def _rewrite_yaml_mapping_keys_non_vars(el, namespace, collection, spec, args, dest):
    translate = []
    for key in el.keys():
        if key not in KEYWORDS_TO_PLUGIN_MAP and is_reserved_name(key):
            continue

        prefix = 'with_'
        if prefix in key:
            prefix_len = len(prefix)

            if not key.startswith(prefix):
                continue
            plugin_name = key[prefix_len:]
            try:
                plugin_namespace, plugin_collection = get_plugin_collection(plugin_name, 'lookup', spec)
                if plugin_collection in COLLECTION_SKIP_REWRITE:
                    raise LookupError

                msg = 'Rewriting to %s.%s.%s' % (plugin_namespace, plugin_collection, plugin_name)
                if args.fail_on_core_rewrite:
                    raise RuntimeError(msg)

                logger.debug(msg)
                translate.append((prefix + get_plugin_fqcn(plugin_namespace, plugin_collection, plugin_name), key))
                integration_tests_add_to_deps((namespace, collection), (plugin_namespace, plugin_collection))
            except LookupError:
                pass

        if isinstance(el[key], Mapping):
            for ns in spec.keys():
                for coll in get_rewritable_collections(ns, spec):
                    if collection == coll:
                        # https://github.com/ansible-community/collection_migration/issues/156
                        continue

                    if key not in get_plugins_from_collection(ns, coll, 'modules', spec):
                        continue

                    new_module_name = get_plugin_fqcn(ns, coll, key)
                    msg = 'Rewriting to %s' % new_module_name
                    if args.fail_on_core_rewrite:
                        raise RuntimeError(msg)

                    logger.debug(msg)
                    translate.append((new_module_name, key))
                    integration_tests_add_to_deps((namespace, collection), (ns, coll))

        if key in KEYWORDS_TO_PLUGIN_MAP:
            _rewrite_yaml_mapping_value(namespace, collection, el, key, KEYWORDS_TO_PLUGIN_MAP[key], spec, args, dest)

        # https://github.com/ansible-community/collection_migration/issues/98
        if key == 'mode' and isinstance(el[key], int):
            # Workaround of PyYAML converting "mode: 0755" (no quotes around 0755) into "mode: 493" (dec).
            # "mode: '0755'" is not converted and stays intact.
            # So if mode is an integer, we know it was converted to its decimal value,
            # in that case convert it back to an octal value and make it a string.
            el[key] = '0' + '%o' % el[key]

    for new_key, old_key in translate:
        el[new_key] = el.pop(old_key)


def _rewrite_yaml_mapping_value(namespace, collection, el, key, plugin_type, spec, args, dest):
    try:
        plugin_namespace, plugin_collection = get_plugin_collection(el[key], plugin_type, spec)
    except LookupError:
        if '{{' in el[key]:
            add_manual_check(key, el[key], dest)
        return

    if plugin_collection in COLLECTION_SKIP_REWRITE:
        return
    new_plugin_name = get_plugin_fqcn(plugin_namespace, plugin_collection, el[key])

    msg = 'Rewriting to %s' % new_plugin_name
    if args.fail_on_core_rewrite:
        raise RuntimeError(msg)

    logger.debug(msg)
    el[key] = new_plugin_name
    integration_tests_add_to_deps((namespace, collection), (plugin_namespace, plugin_collection))


def _rewrite_yaml_mapping_keys_vars(el, namespace, collection, spec, args, dest):
    for key in el.keys():
        if key in VARNAMES_TO_PLUGIN_MAP:
            _rewrite_yaml_mapping_value(namespace, collection, el, key, VARNAMES_TO_PLUGIN_MAP[key], spec, args, dest)


def _rewrite_yaml_mapping_values(el, namespace, collection, spec, args, dest):
    for key, value in el.items():
        if isinstance(value, Mapping):
            if key == 'vars':
                _rewrite_yaml_mapping_keys_vars(el[key], namespace, collection, spec, args, dest)
            if key != 'vars':
                _rewrite_yaml_mapping_keys_non_vars(el[key], namespace, collection, spec, args, dest)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, Mapping):
                    _rewrite_yaml_mapping(el[key][idx], namespace, collection, spec, args, dest)
                else:
                    if key == 'module_blacklist':
                        for ns in spec.keys():
                            for coll in get_rewritable_collections(ns, spec):
                                if item in get_plugins_from_collection(ns, coll, 'modules', spec):
                                    new_plugin_name = get_plugin_fqcn(ns, coll, el[key][idx])
                                    msg = 'Rewriting to %s' % new_plugin_name
                                    if args.fail_on_core_rewrite:
                                        raise RuntimeError(msg)
                                    logger.debug(msg)
                                    el[key][idx] = new_plugin_name
                                    integration_tests_add_to_deps((namespace, collection), (ns, coll))
                    if isinstance(el[key][idx], str):
                        el[key][idx] = _rewrite_yaml_lookup(el[key][idx], namespace, collection, spec, args)
                        el[key][idx] = _rewrite_yaml_filter(el[key][idx], namespace, collection, spec, args)
                        el[key][idx] = _rewrite_yaml_test(el[key][idx], namespace, collection, spec, args)
        elif isinstance(value, str):
            el[key] = _rewrite_yaml_lookup(el[key], namespace, collection, spec, args)
            el[key] = _rewrite_yaml_filter(el[key], namespace, collection, spec, args)
            el[key] = _rewrite_yaml_test(el[key], namespace, collection, spec, args)


def _rewrite_yaml_lookup(value, namespace, collection, spec, args):
    if not ('lookup(' in value or 'query(' in value or 'q(' in value):
        return value

    for ns in spec.keys():
        for coll in get_rewritable_collections(ns, spec):
            for plugin_name in get_plugins_from_collection(ns, coll, 'lookup', spec):
                if plugin_name not in value:
                    continue
                new_plugin_name = get_plugin_fqcn(ns, coll, plugin_name)
                msg = 'Rewriting to %s' % new_plugin_name
                if args.fail_on_core_rewrite:
                    raise RuntimeError(msg)

                logger.debug(msg)
                value = value.replace(plugin_name, new_plugin_name)
                integration_tests_add_to_deps((namespace, collection), (ns, coll))

    return value


def _rewrite_yaml_filter(value, namespace, collection, spec, args):
    if '|' not in value:
        return value
    for ns in spec.keys():
        for coll in get_rewritable_collections(ns, spec):
            for filter_plugin_name in get_plugins_from_collection(ns, coll, 'filter', spec):
                imported_module = import_module('ansible.plugins.filter.' + filter_plugin_name)
                fm = getattr(imported_module, 'FilterModule', None)
                if fm is None:
                    continue
                filters = fm().filters().keys()
                for found_filter in set(match[5] for match in FILTER_RE.findall(value)):
                    if found_filter not in filters:
                        continue
                    new_plugin_name = get_plugin_fqcn(ns, coll, found_filter)
                    msg = 'Rewriting to %s' % new_plugin_name
                    if args.fail_on_core_rewrite:
                        raise RuntimeError(msg)

                    logger.debug(msg)
                    value = value.replace(found_filter, new_plugin_name)
                    integration_tests_add_to_deps((namespace, collection), (ns, coll))

    return value


def _rewrite_yaml_test(value, namespace, collection, spec, args):
    if ' is ' not in value:
        return value
    for ns in spec.keys():
        for coll in get_rewritable_collections(ns, spec):
            for test_plugin_name in get_plugins_from_collection(ns, coll, 'test', spec):
                imported_module = import_module('ansible.plugins.test.' + test_plugin_name)
                tm = getattr(imported_module, 'TestModule', None)
                if tm is None:
                    continue
                tests = tm().tests().keys()
                for found_test in set(match[5] for match in TEST_RE.findall(value)):
                    if found_test not in tests:
                        continue
                    new_plugin_name = get_plugin_fqcn(ns, coll, found_test)
                    msg = 'Rewriting to %s' % new_plugin_name
                    if args.fail_on_core_rewrite:
                        raise RuntimeError(msg)

                    logger.debug(msg)
                    value = value.replace(found_test, new_plugin_name)
                    integration_tests_add_to_deps((namespace, collection), (ns, coll))

    return value


def setup_options(parser):
    parser.add_argument('-s', '--spec', required=True, dest='spec_dir', help='A directory spec with YAML files that describe how to organize collections')
    parser.add_argument('-r', '--refresh', action='store_true', dest='refresh', default=False, help='force refreshing local Ansible checkout')
    parser.add_argument('-t', '--target-dir', dest='vardir', default=VARDIR, help='target directory for resulting collections and rpm')
    parser.add_argument('-p', '--preserve-module-subdirs', action='store_true', dest='preserve_module_subdirs', default=False, help='preserve module subdirs per spec')
    parser.add_argument('--github-app-id', action='store', type=int, dest='github_app_id', default=None if 'GITHUB_APP_IDENTIFIER' in os.environ else 41435,
                        help='Use this GitHub App ID for GH auth',)
    parser.add_argument('--github-app-key-path', action='store', type=str, dest='github_app_key_path',
                        default=None if 'GITHUB_PRIVATE_KEY' in os.environ else '~/Downloads/ansible-migrator.2019-09-18.private-key.pem',
                        help='Use this PEM key file for GH auth. Altertanively, put its contents into `GITHUB_PRIVATE_KEY` env var.',)
    parser.add_argument('--target-github-org', action='store', type=str, dest='target_github_org', default='ansible-collection-migration', help='Push migrated collections to this GH org',)
    parser.add_argument('-P', '--publish-to-github', action='store_true', dest='publish_to_github', default=False, help='Push all migrated collections to their Git remotes')
    parser.add_argument('-m', '--move-plugins', action='store_true', dest='move_plugins', default=False, help='remove plugins from source instead of just copying them')
    parser.add_argument('-M', '--push-migrated-core', action='store_true', dest='push_migrated_core', default=False, help='Push migrated core to the Git repo')
    parser.add_argument('-f', '--fail-on-core-rewrite', action='store_true', dest='fail_on_core_rewrite', default=False,
                        help='Fail on core rewrite. E.g. to verify core does not depend on the collections by running'
                             ' migration against the list of files kept in core: spec must contain the "_core" collection.')
    parser.add_argument('-R', '--skip-tests', action='store_true', dest='skip_tests', default=False, help='Skip tests and rewrite the runtime code only.')
    parser.add_argument('--skip-migration', action='store_true', dest='skip_migration', default=False, help='Skip creating migrated collections.',)
    parser.add_argument('--skip-publish', action='store_true', dest='skip_publish', default=False, help='Skip publishing migrated collections and core repositories.',)


def main():
    parser = argparse.ArgumentParser()

    setup_options(parser)

    args = parser.parse_args()

    # required, so we should always have
    spec = {}

    for spec_file in os.listdir(args.spec_dir):
        if not spec_file.endswith('.yml'):
            logger.debug('skipping %s as it is not a yaml file', spec_file)
            continue
        try:
            spec[os.path.splitext(os.path.basename(spec_file))[0]] = load_spec_file(os.path.join(args.spec_dir, spec_file))
        except Exception as e:
            # warn we skipped spec_file for reasons: e
            raise

    releases_dir = os.path.join(args.vardir, 'releases')
    devel_path = os.path.join(releases_dir, f'{DEVEL_BRANCH}.git')

    global ALL_THE_FILES
    ALL_THE_FILES = checkout_repo(DEVEL_URL, devel_path, refresh=args.refresh)

    if args.skip_migration:
        logger.info('Skipping the migration...')
    else:
        logger.info('Starting the migration...')

        # doeet
        assemble_collections(devel_path, spec, args, args.target_github_org)

        global core
        print('======= Assumed stayed in core =======\n')
        print(yaml.dump(core))

        global manual_check
        print('======= Could not rewrite the following, ' 'please check manually =======\n',)
        print(yaml.dump(dict(manual_check)))

        print(f'See {LOGFILE} for any warnings/errors ' 'that were logged during migration.',)

    if args.skip_publish:
        logger.info('Skipping the publish step...')
        return

    tmp_rsa_key = None
    if args.publish_to_github or args.push_migrated_core:
        logger.info('Starting the publish step...')
        tmp_rsa_key = RSAKey()
        gh_api = GitHubOrgClient(
            args.github_app_id, args.github_app_key_path,
            args.target_github_org,
            deployment_rsa_pub_key=tmp_rsa_key.public_openssh,
        )
        logger.debug('Initialized a temporary RSA key and GitHub API client')

    if args.publish_to_github:
        logger.info('Publishing the migrated collections to GitHub...')
        publish_to_github(
            args.vardir, spec,
            gh_api, tmp_rsa_key,
        )

    if args.push_migrated_core:
        logger.info('Publishing the migrated "Core" to GitHub...')
        push_migrated_core(releases_dir, gh_api, tmp_rsa_key, args.spec_dir)

### main execution

os.makedirs(VARDIR, exist_ok=True)
logzero.logfile(LOGFILE, loglevel=logging.WARNING)

if __name__ == "__main__":
    main()

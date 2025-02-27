#     Copyright 2021, Kay Hayen, mailto:kay.hayen@gmail.com
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
""" Locating modules and package source on disk.

The actual import of a module would already execute code that changes things.
Imagine a module that does ``os.system()``, it would be done during
compilation. People often connect to databases, and these kind of things, at
import time.

Therefore CPython exhibits the interfaces in an ``imp`` module in standard
library, which one can use those to know ahead of time, what file import would
load. For us unfortunately there is nothing in CPython that is easily
accessible and gives us this functionality for packages and search paths
exactly like CPython does, so we implement here a multi step search process
that is compatible.

This approach is much safer of course and there is no loss. To determine if
it's from the standard library, one can abuse the attribute ``__file__`` of the
``os`` module like it's done in ``isStandardLibraryPath`` of this module.

"""


import collections
import hashlib
import imp
import os
import sys
import zipfile

from nuitka import Options, SourceCodeReferences
from nuitka.__past__ import iter_modules
from nuitka.containers.oset import OrderedSet
from nuitka.importing import StandardLibrary
from nuitka.plugins.Plugins import Plugins
from nuitka.PythonVersions import python_version
from nuitka.Tracing import my_print, recursion_logger
from nuitka.utils.AppDirs import getCacheDir
from nuitka.utils.FileOperations import listDir, removeDirectory
from nuitka.utils.Importing import getSharedLibrarySuffixes
from nuitka.utils.ModuleNames import ModuleName
from nuitka.utils.Utils import getOS, isMacOS

from .IgnoreListing import isIgnoreListedNotExistingModule
from .PreloadedPackages import getPreloadedPackagePath, isPreloadedPackagePath

_debug_module_finding = Options.shallExplainImports()

warned_about = set()

# Directory where the main script lives. Should attempt to import from there.
main_path = None


def setMainScriptDirectory(main_dir):
    """Initialize the main script directory.

    We use this as part of the search path for modules.
    """
    # We need to set this from the outside, pylint: disable=global-statement

    global main_path
    main_path = main_dir


def getMainScriptDirectory():
    return main_path


def isPackageDir(dirname):
    """Decide if a directory is a package.

    Before Python3.3 it's required to have a "__init__.py" file, but then
    it became impossible to decide, and for extra fun, there is also the
    extra packages provided via "*.pth" file tricks by "site.py" loading.
    """

    return (
        "." not in os.path.basename(dirname)
        and os.path.isdir(dirname)
        and (
            python_version >= 0x300
            or os.path.isfile(os.path.join(dirname, "__init__.py"))
            or isPreloadedPackagePath(dirname)
        )
    )


def getModuleNameAndKindFromFilename(module_filename):
    """Given a filename, decide the module name and kind.

    Args:
        module_name - file path of the module
    Returns:
        Tuple with the name of the module basename, and the kind of the
        module derived from the file suffix. Can be None, None if is is not a
        known file suffix.
    Notes:
        This doesn't concern itself with packages, that needs to be tracked
        by the using code. It cannot be decided from the filename at all.
    """

    # TODO: This does not handle ".pyw" files it seems.
    if os.path.isdir(module_filename):
        module_name = ModuleName(os.path.basename(module_filename))
        module_kind = "py"
    elif module_filename.endswith(".py"):
        module_name = ModuleName(os.path.basename(module_filename)[:-3])
        module_kind = "py"
    else:
        for suffix in getSharedLibrarySuffixes():
            if module_filename.endswith(suffix):
                module_name = ModuleName(
                    os.path.basename(module_filename)[: -len(suffix)]
                )
                module_kind = "extension"

                break
        else:
            module_kind = None
            module_name = None

    return module_name, module_kind


def isIgnoreListedImportMaker(source_ref):
    return StandardLibrary.isStandardLibraryPath(source_ref.getFilename())


def warnAbout(importing, module_name, level, source_ref):
    # This probably should not be dealt with here
    if module_name == "":
        return

    if not isIgnoreListedNotExistingModule(
        module_name
    ) and not isIgnoreListedImportMaker(source_ref):
        key = module_name, level

        if key not in warned_about:
            warned_about.add(key)

            if Plugins.suppressUnknownImportWarning(
                importing=importing, source_ref=source_ref, module_name=module_name
            ):
                return

            if level == 0:
                level_desc = "as absolute import"
            elif level == -1:
                level_desc = "as relative or absolute import"
            elif level == 1:
                level_desc = "%d package level up" % level
            else:
                level_desc = "%d package levels up" % level

            if _debug_module_finding:
                if importing.getPackageName() is not None:
                    recursion_logger.warning(
                        "%s: Cannot find '%s' in package '%s' %s."
                        % (
                            importing.getSourceReference().getAsString(),
                            module_name,
                            importing.getPackageName().asString(),
                            level_desc,
                        )
                    )
                else:
                    recursion_logger.warning(
                        "%s: Cannot find '%s' %s."
                        % (
                            importing.getSourceReference().getAsString(),
                            module_name,
                            level_desc,
                        )
                    )


def normalizePackageName(module_name):
    # The "os.path" is strangely hacked into the "os" module, dispatching per
    # platform, we either cannot look into it, or we require that we resolve it
    # here correctly.
    if module_name == "os.path":
        module_name = ModuleName(os.path.basename(os.path.__name__))

    return module_name


def findModule(module_name, parent_package, level):
    """Find a module with given package name as parent.

    The package name can be None of course. Level is the same
    as with "__import__" built-in. Warnings are optional.

    Returns:
        Returns a triple of package name the module is in, filename of
        it, which can be a directory for packages, and the location
        method used.
    """
    # We have many branches here, because there are a lot of cases to try.
    # pylint: disable=too-many-branches,too-many-return-statements

    assert type(module_name) is ModuleName, module_name

    if _debug_module_finding:
        my_print(
            "findModule: Enter to search %r in package %r level %s."
            % (module_name, parent_package, level)
        )

    # Do not allow star imports to get here. We just won't find modules with
    # that name, but it would be wasteful.
    assert module_name != "*"

    if level > 1:
        # TODO: Should give a warning and return not found if the levels
        # exceed the package name.
        if parent_package is not None:
            parent_package = parent_package.getRelativePackageName(level)
        else:
            return None, None, "not-found"

    # Try relative imports first if we have a parent package.
    if level != 0 and parent_package is not None:
        if module_name:
            full_name = ModuleName(parent_package + "." + module_name)
        else:
            full_name = ModuleName(parent_package)

        full_name = normalizePackageName(full_name)

        preloaded_path = getPreloadedPackagePath(module_name)

        if preloaded_path is not None:
            for module_filename in preloaded_path:
                if os.path.exists(module_filename):
                    break
            else:
                module_filename = None

            return full_name.getPackageName(), module_filename, "pth"

        try:
            module_filename = _findModule(module_name=full_name)
        except ImportError:
            # For relative import, that is OK, we will still try absolute.
            pass
        else:
            if _debug_module_finding:
                my_print(
                    "findModule: Relative imported module '%s' as '%s' in filename '%s':"
                    % (module_name, full_name, module_filename)
                )

            return full_name.getPackageName(), module_filename, "relative"

    if level < 1 and module_name != "":
        module_name = normalizePackageName(module_name)

        package_name = module_name.getPackageName()

        # Built-in module names must not be searched any further.
        if module_name in sys.builtin_module_names:
            if _debug_module_finding:
                my_print(
                    "findModule: Absolute imported module '%s' in as built-in':"
                    % (module_name,)
                )
            return package_name, None, "built-in"

        # Frozen module names are similar to built-in, but there is no list of
        # them, therefore check loader name. Not useful at this time
        # to make a difference with built-in.
        if python_version >= 0x300 and module_name in sys.modules:
            loader = getattr(sys.modules[module_name], "__loader__", None)

            if (
                loader is not None
                and getattr(loader, "__name__", "") == "FrozenImporter"
            ):
                if _debug_module_finding:
                    my_print(
                        "findModule: Absolute imported module '%s' in as frozen':"
                        % (module_name,)
                    )
                return package_name, None, "built-in"

        preloaded_path = getPreloadedPackagePath(module_name)

        if preloaded_path is not None:
            for module_filename in preloaded_path:
                if os.path.exists(module_filename):
                    break
            else:
                module_filename = None

            return package_name, module_filename, "pth"

        try:
            module_filename = _findModule(module_name=module_name)
        except ImportError:
            # For relative import, that is OK, we will still try absolute.
            pass
        else:
            if _debug_module_finding:
                my_print(
                    "findModule: Found absolute imported module '%s' in filename '%s':"
                    % (module_name, module_filename)
                )

            return package_name, module_filename, "absolute"

    return None, None, "not-found"


# Some platforms are case insensitive.
case_sensitive = not isMacOS() and getOS() != "Windows"

ImportScanFinding = collections.namedtuple(
    "ImportScanFinding", ("found_in", "priority", "full_path", "search_order")
)

# We put here things that are not worth it (Cython is not really used by
# anything really, or where it's know to not have a big # impact, e.g. lxml.

unworthy_namespaces = ("Cython", "lxml")


def _reportCandidates(package_name, module_name, candidate, candidates):
    module_name = (
        package_name.getChildNamed(module_name)
        if package_name is not None
        else module_name
    )

    if (
        candidate.priority == 1
        and Options.shallPreferSourcecodeOverExtensionModules() is None
    ):
        for c in candidates:
            # Don't compare to itself and don't consider unused bytecode a problem.
            if c is candidate or c.priority == 3:
                continue

            if c.search_order == candidate.search_order:
                if not module_name.hasOneOfNamespaces(unworthy_namespaces):

                    recursion_logger.info(
                        """\
Should decide '--prefer-source-code' vs. '--no-prefer-source-code', using \
existing '%s' extension module by default. Candidates were: %s <-> %s."""
                        % (module_name, candidate, c)
                    )


def _findModuleInPath2(package_name, module_name, search_path):
    """This is out own module finding low level implementation.

    Just the full module name and search path are given. This is then
    tasked to raise "ImportError" or return a path if it finds it, or
    None, if it is a built-in.
    """
    # We have many branches here, because there are a lot of cases to try.
    # pylint: disable=too-many-branches,too-many-locals

    # We may have to decide between package and module, therefore build
    # a list of candidates.
    candidates = OrderedSet()

    considered = set()

    # Higher values are lower priority.
    priority_map = {
        imp.PY_COMPILED: 3,
        imp.PY_SOURCE: 0 if Options.shallPreferSourcecodeOverExtensionModules() else 2,
        imp.C_EXTENSION: 1,
    }

    for count, entry in enumerate(search_path):
        # Don't try again, just with an entry of different casing or complete
        # duplicate.
        if os.path.normcase(entry) in considered:
            continue
        considered.add(os.path.normcase(entry))

        package_directory = os.path.join(entry, module_name.asPath())

        # First, check for a package with an init file, that would be the
        # first choice.
        if os.path.isdir(package_directory):
            found = False

            for suffix, _mode, module_type in imp.get_suffixes():
                if module_type == imp.C_EXTENSION:
                    continue

                package_file_name = "__init__" + suffix

                file_path = os.path.join(package_directory, package_file_name)

                if os.path.isfile(file_path):
                    candidates.add(
                        ImportScanFinding(
                            found_in=entry,
                            priority=priority_map[module_type],
                            full_path=package_directory,
                            search_order=count,
                        )
                    )
                    found = True

            if not found and python_version >= 0x300:
                candidates.add(
                    ImportScanFinding(
                        found_in=entry,
                        priority=10,
                        full_path=package_directory,
                        search_order=count + len(search_path),
                    )
                )

        # Then, check out suffixes of all kinds, but only for one directory.
        last_module_type = 0
        for suffix, _mode, module_type in imp.get_suffixes():
            # Use first match per kind only.
            if module_type == last_module_type:
                continue

            full_path = os.path.join(entry, module_name + suffix)

            if os.path.isfile(full_path):
                candidates.add(
                    ImportScanFinding(
                        found_in=entry,
                        priority=4 + priority_map[module_type],
                        full_path=full_path,
                        search_order=count,
                    )
                )
                last_module_type = module_type

    if _debug_module_finding:
        my_print("Candidates:", candidates)

    if candidates:
        # Sort by priority, with entries from same path element coming first, then desired type.
        candidates = sorted(candidates, key=lambda c: (c.search_order, c.priority))

        # On case sensitive systems, no resolution needed.
        if case_sensitive:
            _reportCandidates(
                package_name=package_name,
                module_name=module_name,
                candidate=candidates[0],
                candidates=candidates,
            )
            return candidates[0].full_path
        else:
            for candidate in candidates:
                for fullname, _filename in listDir(candidate[0]):
                    if fullname == candidate.full_path:
                        _reportCandidates(
                            package_name=package_name,
                            module_name=module_name,
                            candidate=candidate,
                            candidates=candidates,
                        )
                        return candidate.full_path

            # Only exact case matches matter, all candidates were ignored,
            # lets just fall through to raising the import error.

    # Nothing found.
    raise ImportError


_egg_files = {}


def _unpackPathElement(path_entry):
    if not path_entry:
        return "."  # empty means current directory

    if os.path.isfile(path_entry) and path_entry.lower().endswith(".egg"):
        if path_entry not in _egg_files:
            with open(path_entry, "rb") as f:
                checksum = hashlib.md5(f.read()).hexdigest()

            target_dir = os.path.join(getCacheDir(), "egg-content", checksum)

            if not os.path.exists(target_dir):
                try:
                    # Not all Python versions allow using with here, pylint: disable=consider-using-with
                    zip_ref = zipfile.ZipFile(path_entry, "r")
                    zip_ref.extractall(target_dir)
                    zip_ref.close()
                except BaseException:
                    removeDirectory(target_dir, ignore_errors=True)
                    raise

            _egg_files[path_entry] = target_dir

        return _egg_files[path_entry]

    return path_entry


def getPackageSearchPath(package_name):
    assert main_path is not None

    if package_name is None:
        return [os.getcwd(), main_path] + [
            _unpackPathElement(path_element) for path_element in sys.path
        ]
    elif "." in package_name:
        parent_package_name, child_package_name = package_name.splitModuleBasename()

        result = []
        for element in getPackageSearchPath(parent_package_name):
            package_dir = os.path.join(element, child_package_name.asPath())

            if isPackageDir(package_dir):
                result.append(package_dir)
                # Hack for "uniconverter". TODO: Move this to plug-in decision. This
                # fails the above test, but at run time should be a package.
            elif package_name == "uniconvertor.app.modules":
                result.append(package_dir)

        return result

    else:
        preloaded_path = getPreloadedPackagePath(package_name)

        if preloaded_path is not None:
            return preloaded_path

        def getPackageDirCandidates(element):
            yield os.path.join(element, package_name.asPath()), False

            # Hack for PyWin32. TODO: Move this "__path__" extensions to be
            # plug-in decisions.
            if package_name == "win32com":
                yield os.path.join(element, "win32comext"), True

        result = []
        for element in getPackageSearchPath(None):
            for package_dir, force_package in getPackageDirCandidates(element):
                if isPackageDir(package_dir) or force_package:
                    result.append(package_dir)

        return result


def _findModuleInPath(module_name):
    package_name, module_name = module_name.splitModuleBasename()

    if _debug_module_finding:
        my_print("_findModuleInPath: Enter", module_name, "in", package_name)

    # The "site" module must be located based on PYTHONPATH before it was
    # executed, while we normally search in PYTHONPATH after it was executed,
    # and on some systems, that fails.
    if package_name is None and module_name == "site":
        candidate = os.environ.get("NUITKA_SITE_FILENAME", "")

        if candidate:
            return candidate

    # Free pass for built-in modules, the need not exist.
    if package_name is None and imp.is_builtin(module_name):
        return None

    search_path = getPackageSearchPath(package_name)

    if _debug_module_finding:
        my_print(
            "_findModuleInPath: Using search path", search_path, "for", package_name
        )

    try:
        module_filename = _findModuleInPath2(
            package_name=package_name, module_name=module_name, search_path=search_path
        )
    except SyntaxError:
        # Warn user, as this is kind of unusual.
        recursion_logger.warning(
            "%s: Module cannot be imported due to syntax errors.",
            module_name if package_name is None else package_name + "." + module_name,
        )

        return None

    if _debug_module_finding:
        my_print("_findModuleInPath: _findModuleInPath2 gave", module_filename)

    return module_filename


module_search_cache = {}


def _findModule(module_name):
    # Not a good module name. TODO: Push this to ModuleName() creation maybe.
    assert module_name != ""

    if _debug_module_finding:
        my_print("_findModule: Enter to search '%s'." % (module_name,))

    assert module_name.getBasename(), module_name

    key = module_name

    if key in module_search_cache:
        result = module_search_cache[key]

        if _debug_module_finding:
            my_print("_findModule: Cached result (see previous call).")

        if result is ImportError:
            raise ImportError

        return result

    module_search_cache[key] = _findModuleInPath(module_name)

    return module_search_cache[key]


def locateModule(module_name, parent_package, level):
    """Locate a module with given package name as parent.

    The package name can be None of course. Level is the same
    as with "__import__" built-in.

    Returns:
        Returns a triple of module name the module has considering
        package containing it, and filename of it which can be a
        directory for packages, and the location method used.
    """
    module_package, module_filename, finding = findModule(
        module_name=module_name,
        parent_package=parent_package,
        level=level,
    )

    assert module_package is None or (
        type(module_package) is ModuleName and module_package != ""
    ), repr(module_package)

    if module_filename is not None:
        module_filename = os.path.normpath(module_filename)

        module_name, module_kind = getModuleNameAndKindFromFilename(module_filename)

        assert module_kind is not None, module_filename

        module_name = ModuleName.makeModuleNameInPackage(module_name, module_package)

    return module_name, module_filename, finding


def locateModules(package_name):
    """Determine child module names.

    Return:
        generator of ModuleName objects
    """
    package_name = ModuleName(package_name)

    module_filename = locateModule(
        module_name=ModuleName(package_name), parent_package=None, level=0
    )[1]

    if module_filename is not None:
        for sub_module in iter_modules([module_filename]):
            yield package_name.getChildNamed(sub_module.name)


def decideModuleSourceRef(filename, module_name, is_main, is_fake, logger):
    # Many branches due to the many cases

    assert type(module_name) is ModuleName, module_name
    assert filename is not None

    is_namespace = False
    is_package = False

    if is_main and os.path.isdir(filename):
        source_filename = os.path.join(filename, "__main__.py")

        if not os.path.isfile(source_filename):
            sys.stderr.write(
                "%s: can't find '__main__' module in '%s'\n"
                % (os.path.basename(sys.argv[0]), filename)
            )
            sys.exit(2)

        filename = source_filename

        main_added = True
    else:
        main_added = False

    if is_fake:
        source_filename = filename

        source_ref = SourceCodeReferences.fromFilename(filename=filename)

        module_name = is_fake

    elif os.path.isfile(filename):
        source_filename = filename

        source_ref = SourceCodeReferences.fromFilename(filename=filename)

    elif isPackageDir(filename):
        is_package = True

        source_filename = os.path.join(filename, "__init__.py")

        if not os.path.isfile(source_filename):
            source_ref = SourceCodeReferences.fromFilename(
                filename=filename
            ).atInternal()
            is_namespace = True
        else:
            source_ref = SourceCodeReferences.fromFilename(
                filename=os.path.abspath(source_filename)
            )

    else:
        logger.sysexit(
            "%s: can't open file '%s'." % (os.path.basename(sys.argv[0]), filename),
            exit_code=2,
        )

    return (
        main_added,
        is_package,
        is_namespace,
        source_ref,
        source_filename,
    )

# cython: profile=True

import cython
from Cython import __version__

from glob import glob
import re, os, shutil, sys

try:
    import hashlib
except ImportError:
    import md5 as hashlib


from distutils.extension import Extension

from Cython import Utils
from Cython.Utils import cached_function, cached_method, path_exists
from Cython.Compiler.Main import Context, CompilationOptions, default_options
    
os.path.join = cached_function(os.path.join)
    
def extended_iglob(pattern):
    if '**/' in pattern:
        seen = set()
        first, rest = pattern.split('**/', 1)
        if first == '':
            first = '.'
        for root in glob(first + "/"):
            for path in extended_iglob(os.path.join(root, rest)):
                if path not in seen:
                    seen.add(path)
                    yield path
            for path in extended_iglob(os.path.join(root, '*', '**', rest)):
                if path not in seen:
                    seen.add(path)
                    yield path
    else:
        for path in glob(pattern):
            yield path

@cached_function
def file_hash(filename):
    path = os.path.normpath(filename.encode("UTF-8"))
    m = hashlib.md5(str(len(path)) + ":")
    m.update(path)
    m.update(open(filename).read())
    return m.hexdigest()

def parse_list(s):
    """
    >>> parse_list("a b c")
    ['a', 'b', 'c']
    >>> parse_list("[a, b, c]")
    ['a', 'b', 'c']
    >>> parse_list('a " " b')
    ['a', ' ', 'b']
    >>> parse_list('[a, ",a", "a,", ",", ]')
    ['a', ',a', 'a,', ',']
    """
    if s[0] == '[' and s[-1] == ']':
        s = s[1:-1]
        delimiter = ','
    else:
        delimiter = ' '
    s, literals = strip_string_literals(s)
    def unquote(literal):
        literal = literal.strip()
        if literal[0] in "'\"":
            return literals[literal[1:-1]]
        else:
            return literal
    return [unquote(item) for item in s.split(delimiter) if item.strip()]

transitive_str = object()
transitive_list = object()

distutils_settings = {
    'name':                 str,
    'sources':              list,
    'define_macros':        list,
    'undef_macros':         list,
    'libraries':            transitive_list,
    'library_dirs':         transitive_list,
    'runtime_library_dirs': transitive_list,
    'include_dirs':         transitive_list,
    'extra_objects':        list,
    'extra_compile_args':   transitive_list,
    'extra_link_args':      transitive_list,
    'export_symbols':       list,
    'depends':              transitive_list,
    'language':             transitive_str,
}

@cython.locals(start=long, end=long)
def line_iter(source):
    start = 0
    while True:
        end = source.find('\n', start)
        if end == -1:
            yield source[start:]
            return
        yield source[start:end]
        start = end+1

class DistutilsInfo(object):

    def __init__(self, source=None, exn=None):
        self.values = {}
        if source is not None:
            for line in line_iter(source):
                line = line.strip()
                if line != '' and line[0] != '#':
                    break
                line = line[1:].strip()
                if line[:10] == 'distutils:':
                    line = line[10:]
                    ix = line.index('=')
                    key = str(line[:ix].strip())
                    value = line[ix+1:].strip()
                    type = distutils_settings[key]
                    if type in (list, transitive_list):
                        value = parse_list(value)
                        if key == 'define_macros':
                            value = [tuple(macro.split('=')) for macro in value]
                    self.values[key] = value
        elif exn is not None:
            for key in distutils_settings:
                if key in ('name', 'sources'):
                    continue
                value = getattr(exn, key, None)
                if value:
                    self.values[key] = value

    def merge(self, other):
        if other is None:
            return self
        for key, value in other.values.items():
            type = distutils_settings[key]
            if type is transitive_str and key not in self.values:
                self.values[key] = value
            elif type is transitive_list:
                if key in self.values:
                    all = self.values[key]
                    for v in value:
                        if v not in all:
                            all.append(v)
                else:
                    self.values[key] = value
        return self

    def subs(self, aliases):
        if aliases is None:
            return self
        resolved = DistutilsInfo()
        for key, value in self.values.items():
            type = distutils_settings[key]
            if type in [list, transitive_list]:
                new_value_list = []
                for v in value:
                    if v in aliases:
                        v = aliases[v]
                    if isinstance(v, list):
                        new_value_list += v
                    else:
                        new_value_list.append(v)
                value = new_value_list
            else:
                if value in aliases:
                    value = aliases[value]
            resolved.values[key] = value
        return resolved

@cython.locals(start=long, q=long, single_q=long, double_q=long, hash_mark=long,
               end=long, k=long, counter=long, quote_len=long)
def strip_string_literals(code, prefix='__Pyx_L'):
    """
    Normalizes every string literal to be of the form '__Pyx_Lxxx',
    returning the normalized code and a mapping of labels to
    string literals.
    """
    new_code = []
    literals = {}
    counter = 0
    start = q = 0
    in_quote = False
    hash_mark = single_q = double_q = -1
    code_len = len(code)
    
    while True:
        if hash_mark < q:
            hash_mark = code.find('#', q)
        if single_q < q:
            single_q = code.find("'", q)
        if double_q < q:
            double_q = code.find('"', q)
        q = min(single_q, double_q)
        if q == -1: q = max(single_q, double_q)

        # We're done.
        if q == -1 and hash_mark == -1:
            new_code.append(code[start:])
            break

        # Try to close the quote.
        elif in_quote:
            if code[q-1] == u'\\':
                k = 2
                while q >= k and code[q-k] == u'\\':
                    k += 1
                if k % 2 == 0:
                    q += 1
                    continue
            if code[q] == quote_type and (quote_len == 1 or (code_len > q + 2 and quote_type == code[q+1] == code[q+2])):
                counter += 1
                label = "%s%s_" % (prefix, counter)
                literals[label] = code[start+quote_len:q]
                full_quote = code[q:q+quote_len]
                new_code.append(full_quote)
                new_code.append(label)
                new_code.append(full_quote)
                q += quote_len
                in_quote = False
                start = q
            else:
                q += 1

        # Process comment.
        elif -1 != hash_mark and (hash_mark < q or q == -1):
            new_code.append(code[start:hash_mark+1])
            end = code.find('\n', hash_mark)
            counter += 1
            label = "%s%s_" % (prefix, counter)
            if end == -1:
                end_or_none = None
            else:
                end_or_none = end
            literals[label] = code[hash_mark+1:end_or_none]
            new_code.append(label)
            if end == -1:
                break
            start = q = end

        # Open the quote.
        else:
            if code_len >= q+3 and (code[q] == code[q+1] == code[q+2]):
                quote_len = 3
            else:
                quote_len = 1
            in_quote = True
            quote_type = code[q]
            new_code.append(code[start:q])
            start = q
            q += quote_len

    return "".join(new_code), literals


dependancy_regex = re.compile(r"(?:^from +([0-9a-zA-Z_.]+) +cimport)|"
                              r"(?:^cimport +([0-9a-zA-Z_.]+)\b)|"
                              r"(?:^cdef +extern +from +['\"]([^'\"]+)['\"])|"
                              r"(?:^include +['\"]([^'\"]+)['\"])", re.M)

@cached_function
def parse_dependencies(source_filename):
    # Actual parsing is way to slow, so we use regular expressions.
    # The only catch is that we must strip comments and string
    # literals ahead of time.
    fh = Utils.open_source_file(source_filename, "rU", error_handling='ignore')
    try:
        source = fh.read()
    finally:
        fh.close()
    distutils_info = DistutilsInfo(source)
    source, literals = strip_string_literals(source)
    source = source.replace('\\\n', ' ').replace('\t', ' ')

    # TODO: pure mode
    cimports = []
    includes = []
    externs  = []
    for m in dependancy_regex.finditer(source):
        cimport_from, cimport, extern, include = m.groups()
        if cimport_from:
            cimports.append(cimport_from)
        elif cimport:
            cimports.append(cimport)
        elif extern:
            externs.append(literals[extern])
        else:
            includes.append(literals[include])
    return cimports, includes, externs, distutils_info


class DependencyTree(object):

    def __init__(self, context):
        self.context = context
        self._transitive_cache = {}

    def parse_dependencies(self, source_filename):
        return parse_dependencies(source_filename)

    @cached_method
    def included_files(self, filename):
        # This is messy because included files are textually included, resolving
        # cimports (and other includes) relative to the including file.
        all = set()
        for include in self.parse_dependencies(filename)[1]:
            include_path = os.path.join(os.path.dirname(filename), include)
            if not path_exists(include_path):
                include_path = self.context.find_include_file(include, None)
            if include_path:
                if '.' + os.path.sep in include_path:
                    include_path = os.path.normpath(include_path)
                all.add(include_path)
            else:
                print("Unable to locate '%s' referenced from '%s'" % (filename, include))
        return all
    
    @cached_method
    def cimports_and_externs(self, filename):
        cimports, includes, externs = self.parse_dependencies(filename)[:3]
        cimports = set(cimports)
        externs = set(externs)
        for include in self.included_files(filename):
            # include file recursion resolved by self.included_files(source_filename)
            deps = self.parse_dependencies(filename)
            cimports.update(deps[0])
            externs.update(deps[2])
        return tuple(cimports), tuple(externs)

    def cimports(self, filename):
        return self.cimports_and_externs(filename)[0]

    @cached_method
    def package(self, filename):
        dir = os.path.dirname(os.path.abspath(str(filename)))
        if dir != filename and path_exists(os.path.join(dir, '__init__.py')):
            return self.package(dir) + (os.path.basename(dir),)
        else:
            return ()

    @cached_method
    def fully_qualifeid_name(self, filename):
        module = os.path.splitext(os.path.basename(filename))[0]
        return '.'.join(self.package(filename) + (module,))

    def find_pxd(self, module, filename=None):
        if module[0] == '.':
            raise NotImplementedError("New relative imports.")
        if filename is not None:
            relative = '.'.join(self.package(filename) + tuple(module.split('.')))
            pxd = self.context.find_pxd_file(relative, None)
            if pxd:
                return pxd
        return self.context.find_pxd_file(module, None)
    find_pxd = cached_method(find_pxd)

    @cached_method
    def cimported_files(self, filename):
        if filename[-4:] == '.pyx' and path_exists(filename[:-4] + '.pxd'):
            pxd_list = [filename[:-4] + '.pxd']
        else:
            pxd_list = []
        for module in self.cimports(filename):
            if module[:7] == 'cython.':
                continue
            pxd_file = self.find_pxd(module, filename)
            if pxd_file is None:
                print("missing cimport: %s" % filename)
                print(module)
            else:
                pxd_list.append(pxd_file)
        return tuple(pxd_list)

    @cached_method
    def immediate_dependencies(self, filename):
        all = set([filename])
        all.update(self.cimported_files(filename))
        all.update(self.included_files(filename))
        return all

    def all_dependencies(self, filename):
        return self.transitive_merge(filename, self.immediate_dependencies, set.union)

    @cached_method
    def timestamp(self, filename):
        return os.path.getmtime(filename)

    def extract_timestamp(self, filename):
        return self.timestamp(filename), filename

    def newest_dependency(self, filename):
        return max([self.extract_timestamp(f) for f in self.all_dependencies(filename)])

    def transitive_fingerprint(self, filename, extra=None):
        try:
            m = hashlib.md5(__version__)
            m.update(file_hash(filename))
            for x in sorted(self.all_dependencies(filename)):
                if os.path.splitext(x)[1] not in ('.c', '.cpp', '.h'):
                    m.update(file_hash(x))
            if extra is not None:
                m.update(str(extra))
            return m.hexdigest()
        except IOError:
            return None

    def distutils_info0(self, filename):
        return self.parse_dependencies(filename)[3]

    def distutils_info(self, filename, aliases=None, base=None):
        return (self.transitive_merge(filename, self.distutils_info0, DistutilsInfo.merge)
            .subs(aliases)
            .merge(base))

    def transitive_merge(self, node, extract, merge):
        try:
            seen = self._transitive_cache[extract, merge]
        except KeyError:
            seen = self._transitive_cache[extract, merge] = {}
        return self.transitive_merge_helper(
            node, extract, merge, seen, {}, self.cimported_files)[0]

    def transitive_merge_helper(self, node, extract, merge, seen, stack, outgoing):
        if node in seen:
            return seen[node], None
        deps = extract(node)
        if node in stack:
            return deps, node
        try:
            stack[node] = len(stack)
            loop = None
            for next in outgoing(node):
                sub_deps, sub_loop = self.transitive_merge_helper(next, extract, merge, seen, stack, outgoing)
                if sub_loop is not None:
                    if loop is not None and stack[loop] < stack[sub_loop]:
                        pass
                    else:
                        loop = sub_loop
                deps = merge(deps, sub_deps)
            if loop == node:
                loop = None
            if loop is None:
                seen[node] = deps
            return deps, loop
        finally:
            del stack[node]

_dep_tree = None
def create_dependency_tree(ctx=None):
    global _dep_tree
    if _dep_tree is None:
        if ctx is None:
            ctx = Context(["."], CompilationOptions(default_options))
        _dep_tree = DependencyTree(ctx)
    return _dep_tree

# This may be useful for advanced users?
def create_extension_list(patterns, exclude=[], ctx=None, aliases=None):
    seen = set()
    deps = create_dependency_tree(ctx)
    to_exclude = set()
    if not isinstance(exclude, list):
        exclude = [exclude]
    for pattern in exclude:
        to_exclude.update(extended_iglob(pattern))
    if not isinstance(patterns, list):
        patterns = [patterns]
    module_list = []
    for pattern in patterns:
        if isinstance(pattern, str):
            filepattern = pattern
            template = None
            name = '*'
            base = None
            exn_type = Extension
        elif isinstance(pattern, Extension):
            filepattern = pattern.sources[0]
            if os.path.splitext(filepattern)[1] not in ('.py', '.pyx'):
                # ignore non-cython modules
                module_list.append(pattern)
                continue
            template = pattern
            name = template.name
            base = DistutilsInfo(exn=template)
            exn_type = template.__class__
        else:
            raise TypeError(pattern)
        for file in extended_iglob(filepattern):
            if file in to_exclude:
                continue
            pkg = deps.package(file)
            if '*' in name:
                module_name = deps.fully_qualifeid_name(file)
            else:
                module_name = name
            if module_name not in seen:
                kwds = deps.distutils_info(file, aliases, base).values
                if base is not None:
                    for key, value in base.values.items():
                        if key not in kwds:
                            kwds[key] = value
                sources = [file]
                if template is not None:
                    sources += template.sources[1:]
                module_list.append(exn_type(
                        name=module_name,
                        sources=sources,
                        **kwds))
                m = module_list[-1]
                seen.add(name)
    return module_list

# This is the user-exposed entry point.
def cythonize(module_list, exclude=[], nthreads=0, aliases=None, quiet=False, force=False, **options):
    if 'include_path' not in options:
        options['include_path'] = ['.']
    c_options = CompilationOptions(**options)
    cpp_options = CompilationOptions(**options); cpp_options.cplus = True
    ctx = c_options.create_context()
    module_list = create_extension_list(
        module_list,
        exclude=exclude,
        ctx=ctx,
        aliases=aliases)
    deps = create_dependency_tree(ctx)
    to_compile = []
    for m in module_list:
        new_sources = []
        for source in m.sources:
            base, ext = os.path.splitext(source)
            if ext in ('.pyx', '.py'):
                if m.language == 'c++':
                    c_file = base + '.cpp'
                    options = cpp_options
                else:
                    c_file = base + '.c'
                    options = c_options
                if os.path.exists(c_file):
                    c_timestamp = os.path.getmtime(c_file)
                else:
                    c_timestamp = -1
                    
                # Priority goes first to modified files, second to direct
                # dependents, and finally to indirect dependents.
                if c_timestamp < deps.timestamp(source):
                    dep_timestamp, dep = deps.timestamp(source), source
                    priority = 0
                else:
                    dep_timestamp, dep = deps.newest_dependency(source)
                    priority = 2 - (dep in deps.immediate_dependencies(source))
                if force or c_timestamp < dep_timestamp:
                    if not quiet:
                        if source == dep:
                            print("Compiling %s because it changed." % source)
                        else:
                            print("Compiling %s because it depends on %s." % (source, dep))
                    if not force and hasattr(options, 'cache'):
                        extra = m.language
                        fingerprint = deps.transitive_fingerprint(source, extra)
                    else:
                        fingerprint = None
                    to_compile.append((priority, source, c_file, fingerprint, quiet, options))
                new_sources.append(c_file)
            else:
                new_sources.append(source)
        m.sources = new_sources
    to_compile.sort()
    if nthreads:
        # Requires multiprocessing (or Python >= 2.6)
        try:
            import multiprocessing
            pool = multiprocessing.Pool(nthreads)
            pool.map(cythonize_one_helper, to_compile)
        except ImportError:
            print("multiprocessing required for parallel cythonization")
            nthreads = 0
    if not nthreads:
        for args in to_compile:
            cythonize_one(*args[1:])
    return module_list

# TODO: Share context? Issue: pyx processing leaks into pxd module
def cythonize_one(pyx_file, c_file, fingerprint, quiet, options=None):
    from Cython.Compiler.Main import compile, default_options
    from Cython.Compiler.Errors import CompileError, PyrexError

    if fingerprint:
        if not os.path.exists(options.cache):
            try:
                os.mkdir(options.cache)
            except:
                if not os.path.exists(options.cache):
                    raise
        fingerprint_file = os.path.join(options.cache, fingerprint + '-' + os.path.basename(c_file))
        if os.path.exists(fingerprint_file):
            if not quiet:
                print("Found compiled %s in cache" % pyx_file)
            os.utime(fingerprint_file, None)
            shutil.copy(fingerprint_file, c_file)
            return
    if not quiet:
        print("Cythonizing %s" % pyx_file)
    if options is None:
        options = CompilationOptions(default_options)
    options.output_file = c_file

    any_failures = 0
    try:
        result = compile([pyx_file], options)
        if result.num_errors > 0:
            any_failures = 1
    except (EnvironmentError, PyrexError), e:
        sys.stderr.write('%s\n' % e)
        any_failures = 1
    if any_failures:
        raise CompileError(None, pyx_file)
    if fingerprint:
        shutil.copy(c_file, fingerprint_file)

def cythonize_one_helper(m):
    return cythonize_one(*m[1:])

import os, linecache

import clang.cindex
from ctypes import *
from fnmatch import fnmatch

from hotdoc.core.base_extension import BaseExtension
from hotdoc.utils.loggable import Loggable
from hotdoc.core.symbols import *
from hotdoc.core.comment_block import comment_from_tag
from hotdoc.lexer_parsers.c_comment_scanner.c_comment_scanner import get_comments
from hotdoc.core.links import Link

def ast_node_is_function_pointer (ast_node):
    if ast_node.kind == clang.cindex.TypeKind.POINTER and \
            ast_node.get_pointee().get_result().kind != \
            clang.cindex.TypeKind.INVALID:
        return True
    return False


class ClangScanner(Loggable):
    def __init__(self, doc_tool, filenames, options, full_scan, full_scan_patterns,
            incremental):
        Loggable.__init__(self)

        self.doc_tool = doc_tool
        if options:
            options = options[0].split(' ')

        index = clang.cindex.Index.create()
        flags = clang.cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES |\
                clang.cindex.TranslationUnit.PARSE_INCOMPLETE |\
                clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD

        self.filenames = filenames
        self.full_scan = full_scan
        self.full_scan_patterns = full_scan_patterns

        # FIXME: er maybe don't do that ?
        args = ["-isystem/usr/lib/clang/3.5.0/include/", "-Wno-attributes"]
        args.extend (options)

        self.symbols = {}
        self.parsed = set({})

        if not full_scan:
            for filename in filenames:
                with open (filename, 'r') as f:
                    cs = get_comments (filename)
                    for c in cs:
                        block = self.doc_tool.raw_comment_parser.parse_comment(c[0], c[1], c[2])
                        if block is not None:
                            self.doc_tool.add_comment(block)

        for filename in self.filenames:
            if filename in self.parsed:
                continue

            do_full_scan = any(fnmatch(filename, p) for p in full_scan_patterns)
            if do_full_scan:
                tu = index.parse(filename, args=args, options=flags)
                for diag in tu.diagnostics:
                    if diag.severity <= 2 and str(diag.location.file) not in self.filenames:
                        self.warning ("Clang issue : %s" % str (diag))
                    else:
                        self.error ("Clang issue : %s" % str (diag))

                self.__parse_file (filename, tu)
                for include in tu.get_includes():
                    self.__parse_file (os.path.abspath(str(include.include)), tu)

        self.info ("%d symbols found" % len (self.symbols))

    def __parse_file (self, filename, tu):
        if filename in self.parsed:
            return

        self.parsed.add (filename)
        start = tu.get_location (filename, 0)
        end = tu.get_location (filename, os.path.getsize(filename))
        extent = clang.cindex.SourceRange.from_locations (start, end)
        cursors = self.__get_cursors(tu, extent)
        if filename in self.filenames:
            self.__create_symbols (cursors, tu)

    # That's the fastest way of obtaining our ast nodes for a given filename
    def __get_cursors (self, tu, extent):
        tokens_memory = POINTER(clang.cindex.Token)()
        tokens_count = c_uint()

        clang.cindex.conf.lib.clang_tokenize(tu, extent, byref(tokens_memory),
                byref(tokens_count))

        count = int(tokens_count.value)

        if count < 1:
            return

        cursors = (clang.cindex.Cursor * count)()
        clang.cindex.conf.lib.clang_annotateTokens (tu, tokens_memory, tokens_count,
                cursors)

        return cursors

    def __create_symbols(self, nodes, tu):
        for node in nodes:
            node._tu = tu
            if node.kind in [clang.cindex.CursorKind.FUNCTION_DECL,
                            clang.cindex.CursorKind.TYPEDEF_DECL,
                            clang.cindex.CursorKind.MACRO_DEFINITION,
                            clang.cindex.CursorKind.VAR_DECL]:
                if self.full_scan:
                    if not node.raw_comment:
                        self.debug ("Discarding symbol %s at location %s as it has no doc" %
                                (node.spelling, str(node.location)))
                        continue

                    block = self.doc_tool.raw_comment_parser.parse_comment \
                                (node.raw_comment, str(node.location.file), 0)
                    self.doc_tool.add_comment(block)

                self.debug ("Found symbol [%s] of kind %s at location %s" %
                        (node.spelling, str(node.kind), str (node.location)))

            if node.spelling in self.symbols:
                continue

            sym = None
            if node.kind == clang.cindex.CursorKind.FUNCTION_DECL:
                sym = self.__create_function_symbol (node)
            elif node.kind == clang.cindex.CursorKind.VAR_DECL:
                sym = self.__create_exported_variable_symbol (node)
            elif node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
                sym = self.__create_macro_symbol (node)
            elif node.kind == clang.cindex.CursorKind.TYPEDEF_DECL:
                sym = self.__create_typedef_symbol (node)

            if sym is not None:
                self.symbols[node.spelling] = sym

    def __apply_qualifiers (self, type_, tokens):
        if type_.is_const_qualified():
            tokens.append ('const ')
        if type_.is_restrict_qualified():
            tokens.append ('restrict ')
        if type_.is_volatile_qualified():
            tokens.append ('volatile ')

    def make_c_style_type_name (self, type_):
        tokens = []
        while (type_.kind == clang.cindex.TypeKind.POINTER):
            self.__apply_qualifiers(type_, tokens)
            tokens.append ('*')
            type_ = type_.get_pointee()

        if type_.kind == clang.cindex.TypeKind.TYPEDEF:
            d = type_.get_declaration ()
            link = Link (None, d.displayname, d.displayname)

            tokens.append (link)
            self.__apply_qualifiers(type_, tokens)
        else:
            tokens.append (type_.spelling + ' ')

        tokens.reverse()
        return tokens

    def __create_callback_symbol (self, node, comment):
        parameters = []

        if comment:
            return_tag = comment.tags.get('returns')
            return_comment = comment_from_tag(return_tag)
        else:
            return_comment = None

        return_value = None

        for child in node.get_children():
            if not return_value:
                t = node.underlying_typedef_type
                res = t.get_pointee().get_result()
                type_tokens = self.make_c_style_type_name (res)
                return_value = ReturnValueSymbol(type_tokens=type_tokens,
                        comment=return_comment)
            else:
                if comment:
                    param_comment = comment.params.get (child.displayname)
                else:
                    param_comment = None

                type_tokens = self.make_c_style_type_name (child.type)
                parameter = ParameterSymbol (argname=child.displayname,
                        type_tokens=type_tokens, comment=param_comment)
                parameters.append (parameter)

        if not return_value:
            return_value = ReturnValueSymbol(type_tokens=[], comment=None)

        sym = self.doc_tool.get_or_create_symbol(CallbackSymbol, parameters=parameters,
                return_value=return_value, comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line)
        return sym

    def __parse_public_fields (self, decl):
        tokens = decl.translation_unit.get_tokens(extent=decl.extent)
        delimiters = []

        filename = str(decl.location.file)

        start = decl.extent.start.line
        end = decl.extent.end.line + 1
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]

        public = True
        if (self.__locate_delimiters(tokens, delimiters)):
            public = False

        children = []
        for child in decl.get_children():
            children.append(child)

        delimiters.reverse()
        if not delimiters:
            return '\n'.join (original_lines), children

        public_children = []
        children = []
        for child in decl.get_children():
            children.append(child)
        children.reverse()
        if children:
            next_child = children.pop()
        else:
            next_child = None
        next_delimiter = delimiters.pop()

        final_text = []

        found_first_child = False

        for i, line in enumerate(original_lines):
            lineno = i + start
            if next_delimiter and lineno == next_delimiter[1]:
                public = next_delimiter[0]
                if delimiters:
                    next_delimiter = delimiters.pop()
                else:
                    next_delimiter = None
                continue

            if not next_child or lineno < next_child.location.line:
                if public or not found_first_child:
                    final_text.append (line)
                continue

            if lineno == next_child.location.line:
                found_first_child = True
                if public:
                    final_text.append (line)
                    public_children.append (next_child)
                while next_child.location.line == lineno:
                    if not children:
                        public = True
                        next_child = None
                        break
                    next_child = children.pop()

        return ('\n'.join(final_text), public_children)

    def __locate_delimiters (self, tokens, delimiters):
        public_pattern = "/*<public>*/"
        private_pattern = "/*<private>*/"
        protected_pattern = "/*<protected>*/"
        had_public = False
        for tok in tokens:
            if tok.kind == clang.cindex.TokenKind.COMMENT:
                comment = ''.join(tok.spelling.split())
                if public_pattern == comment:
                    had_public = True
                    delimiters.append((True, tok.location.line))
                elif private_pattern == comment:
                    delimiters.append((False, tok.location.line))
                elif protected_pattern == comment:
                    delimiters.append((False, tok.location.line))
        return had_public

    def __create_struct_symbol (self, node, comment):
        underlying = node.underlying_typedef_type
        decl = underlying.get_declaration()
        raw_text, public_fields = self.__parse_public_fields (decl)
        members = []
        for field in public_fields:
            if comment:
                member_comment = comment.params.get (field.spelling)
            else:
                member_comment = None

            type_tokens = self.make_c_style_type_name (field.type)
            is_function_pointer = ast_node_is_function_pointer (field.type)
            member = FieldSymbol (is_function_pointer=is_function_pointer,
                    member_name=field.spelling, type_tokens=type_tokens,
                    comment=member_comment)
            members.append (member)

        return self.doc_tool.get_or_create_symbol(StructSymbol, raw_text=raw_text, members=members,
                comment=comment, display_name=node.spelling,
                filename=str(decl.location.file), lineno=decl.location.line)

    def __create_enum_symbol (self, node, comment):
        members = []
        underlying = node.underlying_typedef_type
        decl = underlying.get_declaration()
        for member in decl.get_children():
            if comment:
                member_comment = comment.params.get (member.spelling)
            else:
                member_comment = None
            member_value = member.enum_value
            # FIXME: this is pretty much a macro symbol ?
            member = self.doc_tool.get_or_create_symbol(Symbol, comment=member_comment, display_name=member.spelling,
                    filename=str(member.location.file),
                    lineno=member.location.line)
            member.enum_value = member_value
            members.append (member)

        return self.doc_tool.get_or_create_symbol(EnumSymbol, members=members, comment=comment, display_name=node.spelling,
                filename=str(decl.location.file), lineno=decl.location.line)

    def __create_alias_symbol (self, node, comment):
        type_tokens = self.make_c_style_type_name(node.underlying_typedef_type)
        aliased_type = QualifiedSymbol (type_tokens=type_tokens)
        return self.doc_tool.get_or_create_symbol(AliasSymbol, aliased_type=aliased_type, comment=comment,
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line)

    def __create_typedef_symbol (self, node): 
        t = node.underlying_typedef_type
        comment = self.doc_tool.get_comment (node.spelling)
        if ast_node_is_function_pointer (t):
            sym = self.__create_callback_symbol (node, comment)
        else:
            d = t.get_declaration()
            if d.kind == clang.cindex.CursorKind.STRUCT_DECL:
                sym = self.__create_struct_symbol (node, comment)
            elif d.kind == clang.cindex.CursorKind.ENUM_DECL:
                sym = self.__create_enum_symbol (node, comment)
            else:
                sym = self.__create_alias_symbol (node, comment)
        return sym

    def __create_function_macro_symbol (self, node, comment, original_text): 
        return_value = None
        if comment:
            return_tag = comment.tags.get ('returns')
            if return_tag:
                return_comment = comment_from_tag (return_tag)
                return_value = ReturnValueSymbol (comment=return_comment)

        parameters = []
        if comment:
            for param_name, param_comment in comment.params.iteritems():
                parameter = ParameterSymbol (argname=param_name, comment = param_comment)
                parameters.append (parameter)

        sym = self.doc_tool.get_or_create_symbol(FunctionMacroSymbol, return_value=return_value,
                parameters=parameters, original_text=original_text,
                comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line)
        return sym

    def __create_constant_symbol (self, node, comment, original_text):
        return self.doc_tool.get_or_create_symbol(ConstantSymbol, original_text=original_text, comment=comment,
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line)

    def __create_macro_symbol (self, node):
        l = linecache.getline (str(node.location.file), node.location.line)
        split = l.split()

        start = node.extent.start.line
        end = node.extent.end.line + 1
        filename = str(node.location.file)
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]
        original_text = '\n'.join(original_lines)
        comment = self.doc_tool.get_comment (node.spelling)
        if '(' in split[1]:
            sym = self.__create_function_macro_symbol (node, comment, original_text)
        else:
            sym = self.__create_constant_symbol (node, comment, original_text)

        return sym

    def __create_function_symbol (self, node):
        comment = self.doc_tool.get_comment (node.spelling)
        parameters = []

        if comment:
            return_tag = comment.tags.get('returns')
            return_comment = comment_from_tag(return_tag)
        else:
            return_comment = None

        type_tokens = self.make_c_style_type_name (node.result_type)
        return_value = ReturnValueSymbol (type_tokens=type_tokens,
                comment=return_comment)

        for param in node.get_arguments():
            if comment:
                param_comment = comment.params.get (param.displayname)
            else:
                param_comment = None

            type_tokens = self.make_c_style_type_name (param.type)
            parameter = ParameterSymbol (argname=param.displayname,
                    type_tokens=type_tokens, comment=param_comment)
            parameters.append (parameter)
        sym = self.doc_tool.get_or_create_symbol(FunctionSymbol, parameters=parameters,
                return_value=return_value, comment=comment, display_name=node.spelling,
                filename=str(node.location.file), lineno=node.location.line)

        return sym

    def __create_exported_variable_symbol (self, node):
        l = linecache.getline (str(node.location.file), node.location.line)
        split = l.split()

        start = node.extent.start.line
        end = node.extent.end.line + 1
        filename = str(node.location.file)
        original_lines = [linecache.getline(filename, i).rstrip() for i in range(start,
            end)]
        original_text = '\n'.join(original_lines)
        comment = self.doc_tool.get_comment (node.spelling)

        sym = self.doc_tool.get_or_create_symbol(ExportedVariableSymbol, original_text=original_text,
                comment=self.doc_tool.get_comment (node.spelling),
                display_name=node.spelling, filename=str(node.location.file),
                lineno=node.location.line)
        return sym

class CExtension(BaseExtension):
    EXTENSION_NAME = 'c-extension'

    def __init__(self, doc_tool, args):
        BaseExtension.__init__(self, doc_tool, args)
        self.sources = [os.path.abspath(filename) for filename in
                args.c_sources]
        self.clang_options = args.clang_options

    def setup(self):
        self.scanner = ClangScanner(self.doc_tool, self.stale_source_files, self.clang_options, False,
                ['*.h'], self.doc_tool.incremental)

    def get_source_files(self):
        return self.sources

    @staticmethod
    def add_arguments (parser):
        parser.add_argument ("--c-sources", action="store", nargs="+",
                dest="c_sources", help="C source files to parse",
                default=[], required = True)
        parser.add_argument ("--clang-options", action="store", nargs="+",
                dest="clang_options", help="Flags to pass to clang", default="")
        parser.add_argument ("--end-c-sources", action="store_true")

from nodes import (
    MypyFile, TypeInfo, Node, AssignmentStmt, FuncDef, OverloadedFuncDef,
    TypeDef, VarDef, Var, GDEF, MODULE_REF, Annotation, FuncItem, Import,
    ImportFrom, ImportAll, Block, LDEF, NameExpr, MemberExpr,
    IndexExpr, ParenExpr, TupleExpr, ListExpr, ExpressionStmt, ReturnStmt,
    RaiseStmt, YieldStmt, AssertStmt, OperatorAssignmentStmt, WhileStmt,
    ForStmt, BreakStmt, ContinueStmt, IfStmt, TryStmt, WithStmt, DelStmt,
    GlobalDecl, SuperExpr, DictExpr, CallExpr, RefExpr, OpExpr, UnaryExpr,
    SliceExpr, CastExpr, TypeApplication, Context, SymbolTable,
    SymbolTableNode, TVAR
)
from visitor import NodeVisitor
from errors import Errors
from mtypes import NoneTyp, Callable, Overloaded, Instance, Typ, TypeVar
from checker import function_type
from typeanal import TypeAnalyser


# Semantic analyzer that binds names and does various consistency checks for
# a parse tree. Note that type checking is performed as a separate pass.
class SemanticAnal(NodeVisitor):
    # Library search paths
    list<str> lib_path
    # Module name space
    dict<str, MypyFile> modules
    # Global name space for current module
    SymbolTable globals
    # Names declared using "global" (separate set for each scope)
    list<set<str>> global_decls
    # Module-local name space for current modules
    # TODO not needed?
    SymbolTable module_names
    # Class type variables (the scope is a single class definition)
    SymbolTable class_tvars
    # Local names
    SymbolTable locals
    # All classes, from name to info (TODO needed?)
    TypeInfoMap types
    
    list<str> stack     # Function local/type variable stack
    TypeInfo typ        # TypeInfo of enclosing class (or None)
    bool is_init_method # Are we now analysing __init__?
    bool is_function    # Are we now analysing a function/method?
    bool is_local_ctx   # Are we now analysing a block (not at the
                        # top level or at class body)?
    int loop_depth      # Depth of breakable loops
    str cur_mod_id      # Current module id (or None) (phase 2)
    set<str> imports    # Imported modules (during phase 2 analysis)
    Errors errors       # Keep track of generated errors
    
    # Create semantic analyzer. Use libPath to search for modules, and report
    # compile errors using the Errors instance.
    void __init__(self, list<str> lib_path, Errors errors):
        self.stack = [None]
        self.imports = set()
        self.typ = None
        self.loop_depth = 0
        self.types = TypeInfoMap()
        self.lib_path = lib_path
        self.errors = errors
        self.modules = {}
        self.class_tvars = None
        self.is_init_method = False
        self.is_function = False
        self.is_local_ctx = False
    
    
    # First pass of semantic analysis
    
    
    # Perform the first analysis pass. Resolve the full names of definitions
    # and construct type info structures, but do not resolve inter-definition
    # references such as base classes.
    void anal_defs(self, list<Node> defs, str fnam, str mod_id):
        self.cur_mod_id = mod_id
        self.errors.set_file(fnam)
        self.globals = SymbolTable()
        self.global_decls = [set()]
        for d in defs:
            if isinstance(d, AssignmentStmt):
                self.anal_assignment_stmt((AssignmentStmt)d)
            elif isinstance(d, FuncDef):
                self.anal_func_def((FuncDef)d)
            elif isinstance(d, OverloadedFuncDef):
                self.anal_overloaded_func_def((OverloadedFuncDef)d)
            elif isinstance(d, TypeDef):
                self.anal_type_def((TypeDef)d)
            elif isinstance(d, VarDef):
                self.anal_var_def((VarDef)d)
        # Add implicit definition of 'None' to builtins, as we cannot define a
        # variable with a None type explicitly.
        if mod_id == 'builtins':
            none_def = VarDef([(Var('None'), NoneTyp())], True)
            defs.append(none_def)
            self.anal_var_def(none_def)
    
    void anal_assignment_stmt(self, AssignmentStmt s):
        for lval in s.lvalues:
            self.analyse_lvalue(lval, False, True)
    
    void anal_func_def(self, FuncDef d):
        self.check_no_global(d.name(), d, True)
        d._full_name = self.qualified_name(d.name())
        self.globals[d.name()] = SymbolTableNode(GDEF, d, self.cur_mod_id)
    
    void anal_overloaded_func_def(self, OverloadedFuncDef d):
        self.check_no_global(d.name(), d)
        d._full_name = self.qualified_name(d.name())
        self.globals[d.name()] = SymbolTableNode(GDEF, d, self.cur_mod_id)
    
    void anal_type_def(self, TypeDef d):
        self.check_no_global(d.name, d)
        d.full_name = self.qualified_name(d.name)
        info = TypeInfo({}, {}, d)
        info.set_line(d.line)
        self.types[d.full_name] = info
        d.info = info
        self.globals[d.name] = SymbolTableNode(GDEF, info, self.cur_mod_id)
    
    void anal_var_def(self, VarDef d):
        for v, t in d.items:
            self.check_no_global(v.name(), d)
            v._full_name = self.qualified_name(v.name())
            self.globals[v.name()] = SymbolTableNode(GDEF, v, self.cur_mod_id)
    
    
    # Second pass of semantic analysis
    
    
    # Do the bulk of semantic analysis in this second and final semantic
    # analysis pass (other than type checking).
    
    
    void visit_file(self, MypyFile file_node, str fnam):
        self.errors.set_file(fnam)
        self.globals = file_node.names
        self.module_names = SymbolTable()
        self.cur_mod_id = file_node.full_name()
        
        if 'builtins' in self.modules:
            self.globals['__builtins__'] = SymbolTableNode(
                MODULE_REF, self.modules['builtins'], self.cur_mod_id)
        
        defs = file_node.defs
        for d in defs:
            d.accept(self)
    
    void visit_func_def(self, FuncDef defn):
        if self.typ is not None:
            defn.info = self.typ
            if not defn.is_overload:
                if defn.name() in self.typ.methods:
                    self.name_already_defined(defn.name(), defn)
                self.typ.methods[defn.name()] = defn
            if defn.name() == '__init__':
                self.is_init_method = True
            if defn.args == []:
                self.fail('Method must have at least one argument', defn)
        
        self.errors.set_function(defn.name())
        self.analyse_function(defn)
        self.errors.set_function(None)
        self.is_init_method = False
    
    void visit_overloaded_func_def(self, OverloadedFuncDef defn):
        list<Callable> t = []
        for f in defn.items:
            f.is_overload = True
            f.accept(self)
            t.append((Callable)function_type(f))
        defn.typ = Annotation(Overloaded(t))
        defn.typ.set_line(defn.line)
        
        if self.typ is not None:
            self.typ.methods[defn.name()] = defn
            defn.info = self.typ
    
    void analyse_function(self, FuncItem defn):
        self.enter()
        self.add_func_type_variables_to_symbol_table(defn)
        if defn.typ is not None:
            defn.typ.accept(self)
            if isinstance(defn, FuncDef):
                fdef = (FuncDef)defn
                if self.typ is not None:
                    defn.typ.typ = ((Callable)defn.typ.typ).with_name(
                        '"{}" of "{}"'.format(fdef.name, self.typ.name))
                else:
                    defn.typ.typ = ((Callable)defn.typ.typ).with_name(
                        '"{}"'.format(fdef.name))
                if self.typ and ((Callable)defn.typ.typ).arg_types != []:
                    ((Callable)defn.typ.typ).arg_types[0] = self_type(
                        fdef.info)
        for init in defn.init:
            if init is not None:
                init.rvalue.accept(self)
        for v in defn.args:
            self.add_local(v, defn)
        if defn.var_arg is not None:
            self.add_local(defn.var_arg, defn)
        for init_ in defn.init:
            if init_ is not None:
                init_.lvalues[0].accept(self)
        
        # The first argument of a method is self.
        if self.typ is not None and len(defn.args) > 0:
            defn.args[0].is_self = True
        
        defn.body.accept(self)
        self.leave()
    
    void add_func_type_variables_to_symbol_table(self, FuncItem defn):
        if defn.typ is not None:
            tt = defn.typ.typ
            names = self.type_var_names()
            items = ((Callable)tt).variables.items
            for i in range(len(items)):
                name = items[i].name
                if name in names:
                    self.name_already_defined(name, defn)
                self.add_type_var(self.locals, name, -i - 1)
                names.add(name)
    
    set<str> type_var_names(self):
        if self.typ is None:
            return set()
        else:
            return set(self.typ.type_vars)
    
    void add_type_var(self, SymbolTable scope, str name, int id):
        scope[name] = SymbolTableNode(TVAR, None, None, None, id)
    
    void visit_type_def(self, TypeDef defn):
        self.typ = defn.info
        self.add_class_type_variables_to_symbol_table(self.typ)
        has_base_class = False
        for i in range(len(defn.base_types)):
            defn.base_types[i] = self.anal_type(defn.base_types[i])
            self.typ.bases.append(defn.base_types[i])
            has_base_class = has_base_class or self.is_instance_type(
                                                        defn.base_types[i])
        if (not defn.is_interface and not has_base_class and
                defn.full_name != 'builtins.object'):
            defn.base_types.insert(0, self.object_type())
        if defn.base_types != []:
            bt = defn.base_types
            if isinstance(bt[0], Instance):
                defn.info.base = ((Instance)bt[0]).typ
            for t in bt[1:]:
                if isinstance(t, Instance):
                    defn.info.add_interface(((Instance)t).typ)
        defn.defs.accept(self)
        self.class_tvars = None
        self.typ = None
    
    Typ object_type(self):
        sym = self.lookup_qualified('__builtins__.object', None)
        return Instance((TypeInfo)sym.node, [])
    
    bool is_instance_type(self, Typ t):
        return isinstance(t, Instance) and not ((Instance)t).typ.is_interface
    
    void add_class_type_variables_to_symbol_table(self, TypeInfo info):
        vars = info.type_vars
        if vars != []:
            self.class_tvars = SymbolTable()
            for i in range(len(vars)):
                self.add_type_var(self.class_tvars, vars[i], i + 1)
    
    void visit_annotation(self, Annotation ann):
        ann.typ = self.anal_type(ann.typ)
    
    void visit_import(self, Import i):
        for id, as_id in i.ids:
            if as_id != id:
                m = self.modules[id]
                self.globals[as_id] = SymbolTableNode(MODULE_REF, m,
                                                      self.cur_mod_id)
            else:
                base = id.split('.')[0]
                m = self.modules[base]
                self.globals[base] = SymbolTableNode(MODULE_REF, m,
                                                     self.cur_mod_id)
    
    void visit_import_from(self, ImportFrom i):
        m = self.modules[i.id]
        for id, as_id in i.names:
            node = m.names.get(id, None)
            if node is not None:
                self.globals[as_id] = SymbolTableNode(node.kind, node.node,
                                                      self.cur_mod_id)
            else:
                self.fail("Module has no attribute '{}'".format(id), i)
    
    void visit_import_all(self, ImportAll i):
        m = self.modules[i.id]
        for name, node in m.names.items():
            if not name.startswith('_'):
                self.globals[name] = SymbolTableNode(node.kind, node.node,
                                                     self.cur_mod_id)
    
    
    # Statements
    
    
    void visit_block(self, Block b):
        for s in b.body:
            s.accept(self)
    
    void visit_block_maybe(self, Block b):
        if b is not None:
            self.visit_block(b)
    
    void visit_var_def(self, VarDef defn):
        for i in range(len(defn.items)):
            defn.items[i] = (defn.items[i][0],
                             self.anal_type(defn.items[i][1]))
            if defn.items[i][1] is not None:
                defn.items[i][0].typ = Annotation(defn.items[i][1])
        
        for v, t in defn.items:
            if self.locals is not None:
                defn.kind = LDEF
                self.add_local(v, defn)
            elif self.typ is not None:
                v.info = self.typ
                self.typ.vars[v.name()] = v
            elif v.name not in self.globals:
                defn.kind = GDEF
                self.add_var(v, defn)
        
        if defn.init is not None:
            defn.init.accept(self)
    
    Typ anal_type(self, Typ t):
        if t is not None:
            a = TypeAnalyser(self.lookup_qualified, self.fail)
            return t.accept(a)
        else:
            return None
    
    void visit_assignment_stmt(self, AssignmentStmt s):
        for lval in s.lvalues:
            self.analyse_lvalue(lval)
        s.rvalue.accept(self)
    
    void analyse_lvalue(self, Node lval, bool nested=False,
                        bool add_defs=False):
        if isinstance(lval, NameExpr):
            n = (NameExpr)lval
            if add_defs and n.name not in self.globals:
                v = Var(n.name)
                v._full_name = self.qualified_name(n.name)
                n.node = v
                n.is_def = True
                self.globals[n.name] = SymbolTableNode(GDEF, v,
                                                       self.cur_mod_id)
            elif isinstance(n.node, Var) and n.is_def:
                v = (Var)n.node
                self.module_names[v.name()] = SymbolTableNode(GDEF, v,
                                                              self.cur_mod_id)
            elif (self.locals is not None and n.name not in self.locals and
                  n.name not in self.global_decls[-1]):
                v = Var(n.name)
                n.node = v
                n.is_def = True
                n.kind = LDEF
                self.add_local(v, n)
            else:
                lval.accept(self)
        elif isinstance(lval, MemberExpr):
            if not add_defs:
                self.analyse_member_lvalue((MemberExpr)lval)
        elif isinstance(lval, IndexExpr):
            if not add_defs:
                lval.accept(self)
        elif isinstance(lval, ParenExpr):
            self.analyse_lvalue(((ParenExpr)lval).expr, nested, add_defs)
        elif (isinstance(lval, TupleExpr) or
              isinstance(lval, ListExpr)) and not nested:
            items = ((any)lval).items
            for i in items:
                self.analyse_lvalue(i, True, add_defs)
        else:
            self.fail('Invalid assignment target', lval)
    
    void analyse_member_lvalue(self, MemberExpr lval):
        lval.accept(self)
        if self.is_init_method and isinstance(lval.expr, NameExpr):
            node = ((NameExpr)lval.expr).node
            if (isinstance(node, Var) and ((Var)node).is_self and
                    lval.name not in self.typ.vars):
                lval.is_def = True
                v = Var(lval.name)
                v.info = self.typ
                lval.def_var = v
                self.typ.vars[lval.name] = v
    
    void visit_expression_stmt(self, ExpressionStmt s):
        s.expr.accept(self)
    
    void visit_return_stmt(self, ReturnStmt s):
        if self.locals is None:
            self.fail("'return' outside function", s)
        if s.expr is not None:
            s.expr.accept(self)
    
    void visit_raise_stmt(self, RaiseStmt s):
        if s.expr is not None:
            s.expr.accept(self)
    
    void visit_yield_stmt(self, YieldStmt s):
        if self.locals is None:
            self.fail("'yield' outside function", s)
        if s.expr is not None:
            s.expr.accept(self)
    
    void visit_assert_stmt(self, AssertStmt s):
        if s.expr is not None:
            s.expr.accept(self)
    
    void visit_operator_assignment_stmt(self, OperatorAssignmentStmt s):
        s.lvalue.accept(self)
        s.rvalue.accept(self)
    
    void visit_while_stmt(self, WhileStmt s):
        s.expr.accept(self)
        self.loop_depth += 1
        s.body.accept(self)
        self.loop_depth -= 1
        self.visit_block_maybe(s.else_body)
    
    void visit_for_stmt(self, ForStmt s):
        for i in range(len(s.types)):
            t = s.types[i]
            if t is not None:
                t.accept(self)
                s.index[i].typ = t
        
        # Report error if only some of the loop variables have annotations.
        if s.types != [None] * len(s.types) and None in s.types:
            self.fail('Cannot mix unannotated and annotated loop variables', s)
        
        s.expr.accept(self)
        for ind in s.index:
            self.add_var(ind, s)
        self.loop_depth += 1
        self.visit_block(s.body)
        self.loop_depth -= 1
        
        self.visit_block_maybe(s.else_body)
    
    void visit_break_stmt(self, BreakStmt s):
        if self.loop_depth == 0:
            self.fail("'break' outside loop", s)
    
    void visit_continue_stmt(self, ContinueStmt s):
        if self.loop_depth == 0:
            self.fail("'continue' outside loop", s)
    
    void visit_if_stmt(self, IfStmt s):
        for i in range(len(s.expr)):
            s.expr[i].accept(self)
            self.visit_block(s.body[i])
        self.visit_block_maybe(s.else_body)
    
    void visit_try_stmt(self, TryStmt s):
        s.body.accept(self)
        for i in range(len(s.types)):
            if s.types[i] is not None:
                s.types[i].accept(self)
            if s.vars[i] is not None:
                self.add_var(s.vars[i], s.vars[i])
            s.handlers[i].accept(self)
        self.visit_block_maybe(s.else_body)
        self.visit_block_maybe(s.finally_body)
    
    void visit_with_stmt(self, WithStmt s):
        for e in s.expr:
            e.accept(self)
        for n in s.name:
            if n is not None:
                self.add_var(n, s)
        self.visit_block(s.body)
    
    void visit_del_stmt(self, DelStmt s):
        s.expr.accept(self)
        if not isinstance(s.expr, IndexExpr):
            self.fail('Invalid delete target', s)
    
    void visit_global_decl(self, GlobalDecl g):
        for n in g.names:
            self.global_decls[-1].add(n)
    
    
    # Expressions
    
    
    void visit_name_expr(self, NameExpr expr):
        n = self.lookup(expr.name, expr)
        if n is not None:
            if n.kind == TVAR:
                self.fail("'{}' is a type variable and only valid in type "
                          "context".format(expr.name), expr)
            else:
                expr.kind = n.kind
                expr.node = ((Node)n.node)
                expr.full_name = n.full_name()
    
    void visit_super_expr(self, SuperExpr expr):
        if self.typ is None:
            self.fail('"super" used outside class', expr)
            return 
        expr.info = self.typ
    
    void visit_tuple_expr(self, TupleExpr expr):
        for item in expr.items:
            item.accept(self)
        if expr.types is not None:
            for i in range(len(expr.types)):
                expr.types[i] = self.anal_type(expr.types[i])
    
    void visit_list_expr(self, ListExpr expr):
        for item in expr.items:
            item.accept(self)
        expr.typ = self.anal_type(expr.typ)
    
    void visit_dict_expr(self, DictExpr expr):
        for key, value in expr.items:
            key.accept(self)
            value.accept(self)
        expr.key_type = self.anal_type(expr.key_type)
        expr.value_type = self.anal_type(expr.value_type)
    
    void visit_paren_expr(self, ParenExpr expr):
        expr.expr.accept(self)
    
    void visit_call_expr(self, CallExpr expr):
        expr.callee.accept(self)
        for a in expr.args:
            a.accept(self)
        for n, v in expr.keyword_args:
            v.accept(self)
    
    void visit_member_expr(self, MemberExpr expr):
        base = expr.expr
        base.accept(self)
        # Bind references to module attributes.
        if isinstance(base, RefExpr) and ((RefExpr)base).kind == MODULE_REF:
            names = ((MypyFile)((RefExpr)base).node).names
            n = names.get(expr.name, None)
            if n is not None:
                expr.kind = n.kind
                expr.full_name = n.full_name()
                expr.node = ((Node)n.node)
            else:
                self.fail("Module has no attribute '{}'".format(expr.name),
                          expr)
    
    void visit_op_expr(self, OpExpr expr):
        expr.left.accept(self)
        expr.right.accept(self)
    
    void visit_unary_expr(self, UnaryExpr expr):
        expr.expr.accept(self)
    
    void visit_index_expr(self, IndexExpr expr):
        expr.base.accept(self)
        expr.index.accept(self)
    
    void visit_slice_expr(self, SliceExpr expr):
        if expr.begin_index is not None:
            expr.begin_index.accept(self)
        if expr.end_index is not None:
            expr.end_index.accept(self)
        if expr.stride is not None:
            expr.stride.accept(self)
    
    void visit_cast_expr(self, CastExpr expr):
        expr.expr.accept(self)
        expr.typ = self.anal_type(expr.typ)
    
    void visit_type_application(self, TypeApplication expr):
        expr.expr.accept(self)
        for i in range(len(expr.types)):
            expr.types[i] = self.anal_type(expr.types[i])
    
    
    # Helpers
    
    
    SymbolTableNode lookup(self, str name, Context ctx):
        if name in self.global_decls[-1]:
            if name in self.globals:
                return self.globals[name]
            else:
                self.name_not_defined(name, ctx)
        elif self.locals is not None and name in self.locals:
            return self.locals[name]
        elif self.class_tvars is not None and name in self.class_tvars:
            return self.class_tvars[name]
        elif name in self.globals:
            return self.globals[name]
        else:
            b = self.globals.get('__builtins__', None)
            if b is not None:
                table = ((MypyFile)b.node).names
                if name in table:
                    return table[name]
            self.name_not_defined(name, ctx)
    
    SymbolTableNode lookup_qualified(self, str name, Context ctx):
        if '.' not in name:
            return self.lookup(name, ctx)
        else:
            parts = name.split('.')
            SymbolTableNode n = self.lookup(parts[0], ctx)
            if n is not None:
                for i in range(1, len(parts)):
                    n = ((MypyFile)n.node).names.get(parts[i], None)
                    if n is None:
                        self.name_not_defined(name, ctx)
            return n
    
    str qualified_name(self, str n):
        return self.cur_mod_id + '.' + n
    
    void enter(self):
        self.locals = SymbolTable()
        self.global_decls.append(set())
    
    void leave(self):
        self.locals = None
        self.global_decls.pop()
    
    void add_var(self, Var v, Context ctx):
        if self.locals is not None:
            self.add_local(v, ctx)
        else:
            self.globals[v.name()] = SymbolTableNode(GDEF, v, self.cur_mod_id)
            v._full_name = self.qualified_name(v.name())
    
    void add_local(self, Var v, Context ctx):
        if v.name() in self.locals:
            self.name_already_defined(v.name(), ctx)
        v._full_name = v.name()
        self.locals[v.name()] = SymbolTableNode(LDEF, v)
    
    void check_no_global(self, str n, Context ctx, bool is_func=False):
        if n in self.globals:
            if is_func and isinstance(self.globals[n].node, FuncDef):
                self.fail(("Name '{}' already defined (overload variants "
                           "must be next to each other)").format(n), ctx)
            else:
                self.name_already_defined(n, ctx)
    
    void name_not_defined(self, str name, Context ctx):
        self.fail("Name '{}' is not defined".format(name), ctx)
    
    void name_already_defined(self, str name, Context ctx):
        self.fail("Name '{}' already defined".format(name), ctx)
    
    void fail(self, str msg, Context ctx):
        self.errors.report(ctx.get_line(), msg)


# For a non-generic type, return instance type representing the type.
# For a generic G type with parameters T1, .., Tn, return G<T1, ..., Tn>.
Instance self_type(TypeInfo typ):
    list<Typ> tv = []
    for i in range(len(typ.type_vars)):
        tv.append(TypeVar(typ.type_vars[i], i + 1))
    return Instance(typ, tv)


class TypeInfoMap(dict<str, TypeInfo>):
    str __str__(self):
        list<str> a = ['TypeInfoMap(']
        for x, y in sorted(self.items()):
            if isinstance(x, str) and not x.startswith('builtins.'):
                ti = ('\n' + '  ').join(str(y).split('\n'))
                a.append('  {} : {}'.format(x, ti))
        a[-1] += ')'
        return '\n'.join(a)
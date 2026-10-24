#!/usr/bin/env node
// Platform V2 audit: TypeScript/TSX AST extractor (routes, request call sites, vitest call sites).
//
// stdin : JSON {"files": [{"path": "src/App.tsx", "text": "...", "roles": ["routes"|"api"|"tests"]}]}
// argv[2]: directory of an installed `typescript` package (the parser the project itself ships).
// stdout: JSON. Nothing is read from disk except the typescript package: all source text arrives on
// stdin, so the Python caller controls (and records) exactly which immutable blobs were analysed.
//
// Scope and limits are deliberately narrow (this is not a general JS analyser):
//   * Route: JSX <Route> elements only. createBrowserRouter/useRoutes object configs are reported as
//     unsupported, never silently ignored.
//   * Requests: fetch(), fetchEventSource(), new WebSocket/EventSource(), plus *wrapper functions*
//     discovered from their definitions when a wrapper-like parameter (WRAPPER_PARAM_NAMES) flows into a
//     request URL. Calls whose URL/method cannot be resolved statically go to `unresolved`.
//   * Same-file const strings and template literals are resolved; anything else becomes a dynamic
//     placeholder or an unresolved entry. No data-flow, no cross-module constant propagation.
import { createRequire } from "node:module";
import path from "node:path";

const WRAPPER_PARAM_NAMES = new Set(["path", "url", "endpoint", "pathname", "href", "query"]);
const ROUTE_WRAPPER_TAGS = new Set(["Suspense", "Fragment", "RouteFallback", "ErrorBoundary"]);
const URL_ENCODERS = new Set(["encodeURIComponent", "encodeURI", "String", "Number"]);
const PRIMITIVE_CALLS = { fetch: "fetch", fetchEventSource: "sse" };
const PRIMITIVE_NEW = { WebSocket: "websocket", EventSource: "sse" };

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

const tsDir = process.argv[2];
if (!tsDir) {
  console.error("usage: platform_v2_ts_ast.mjs <typescript-package-dir>");
  process.exit(64);
}
const ts = createRequire(import.meta.url)(path.resolve(tsDir));

function parse(file) {
  const kind = file.path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file.path, file.text, ts.ScriptTarget.Latest, true, kind);
  const errors = (sf.parseDiagnostics || []).map((d) => {
    const lc = sf.getLineAndCharacterOfPosition(d.start ?? 0);
    return { line: lc.line + 1, column: lc.character + 1, message: ts.flattenDiagnosticMessageText(d.messageText, "\n") };
  });
  return { sf, errors };
}

const pos = (sf, node) => {
  const lc = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  return { line: lc.line + 1, column: lc.character + 1 };
};
const snippet = (sf, node, max = 240) => node.getText(sf).replace(/\s+/g, " ").slice(0, max);

function unwrap(n) {
  while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n) || ts.isSatisfiesExpression?.(n) || ts.isTypeAssertionExpression(n))) {
    n = n.expression;
  }
  return n;
}

// ---------------------------------------------------------------- routes

function extractRoutes(sf) {
  const routes = [];
  const unsupported = [];
  const lazyComponents = new Set();
  sf.forEachChild(function scan(n) {
    if (ts.isVariableDeclaration(n) && n.initializer) {
      const init = unwrap(n.initializer);
      if (ts.isCallExpression(init) && ts.isIdentifier(init.expression) && init.expression.text === "lazy" && ts.isIdentifier(n.name)) {
        lazyComponents.add(n.name.text);
      }
    }
    n.forEachChild(scan);
  });

  const tagName = (el) => el.tagName.getText(sf);
  const attrOf = (attrs, name) => attrs.properties.find((p) => ts.isJsxAttribute(p) && p.name.getText(sf) === name);

  function collectTags(node, out) {
    if (ts.isJsxSelfClosingElement(node)) out.push(tagName(node));
    if (ts.isJsxOpeningElement(node)) out.push(tagName(node));
    node.forEachChild((c) => collectTags(c, out));
  }

  function joinPath(parentFull, p) {
    if (p.startsWith("/")) return p;
    const base = parentFull.replace(/\/+$/, "");
    return `${base}/${p}`.replace(/\/+/g, "/") || "/";
  }

  function visit(node, parent) {
    const isSelf = ts.isJsxSelfClosingElement(node);
    const isElem = ts.isJsxElement(node);
    const open = isElem ? node.openingElement : isSelf ? node : null;
    if (open && tagName(open) === "Route") {
      const attrs = open.attributes;
      const pathAttr = attrOf(attrs, "path");
      const indexAttr = attrOf(attrs, "index");
      const elementAttr = attrOf(attrs, "element");
      const rec = {
        index: routes.length,
        parent_index: parent ? parent.index : null,
        depth: parent ? parent.depth + 1 : 0,
        ...pos(sf, open),
        path_kind: "none",
        path_raw: null,
        path: null,
        index_route: Boolean(indexAttr),
        has_children: isElem && node.children.some((c) => ts.isJsxElement(c) || ts.isJsxSelfClosingElement(c)),
        element_tags: [],
        component: null,
        is_lazy: false,
        is_redirect: false,
        redirect_to: null,
        full_path: null,
        raw: snippet(sf, open),
      };
      routes.push(rec);
      if (pathAttr) {
        const init = pathAttr.initializer;
        rec.path_raw = init ? init.getText(sf) : null;
        if (init && ts.isStringLiteral(init)) {
          rec.path_kind = "literal";
          rec.path = init.text;
        } else if (init && ts.isJsxExpression(init) && init.expression && (ts.isStringLiteral(init.expression) || ts.isNoSubstitutionTemplateLiteral(init.expression))) {
          rec.path_kind = "literal";
          rec.path = init.expression.text;
        } else {
          rec.path_kind = "expression";
        }
      }
      if (elementAttr) {
        collectTags(elementAttr, rec.element_tags);
        const real = rec.element_tags.filter((t) => !ROUTE_WRAPPER_TAGS.has(t));
        rec.component = real[0] ?? null;
        rec.is_lazy = rec.component ? lazyComponents.has(rec.component) : false;
        if (rec.element_tags.includes("Navigate")) {
          rec.is_redirect = true;
          const nav = (function find(n) {
            if ((ts.isJsxSelfClosingElement(n) || ts.isJsxOpeningElement(n)) && tagName(n) === "Navigate") return n;
            let r = null;
            n.forEachChild((c) => { r = r ?? find(c); });
            return r;
          })(elementAttr);
          const to = nav ? attrOf(nav.attributes, "to") : null;
          rec.redirect_to = to && to.initializer && ts.isStringLiteral(to.initializer) ? to.initializer.text : null;
        }
      }
      const parentFull = parent ? parent.full_path ?? "" : "";
      rec.full_path = rec.path_kind === "literal" ? joinPath(parentFull, rec.path) : rec.path_kind === "none" ? parentFull : null;
      // classification (mutually exclusive)
      if (rec.path_kind === "expression") rec.kind = "path_expression_unresolved";
      else if (rec.path_kind === "literal") rec.kind = rec.path.includes("*") ? "wildcard" : rec.path.includes(":") ? "dynamic" : "concrete";
      else if (rec.index_route) rec.kind = "index";
      else if (rec.has_children) rec.kind = "layout";
      else rec.kind = "pathless_leaf";
      const next = rec;
      if (isElem) node.children.forEach((c) => visit(c, next));
      attrs.forEachChild((c) => visit(c, next));
      return;
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && ["createBrowserRouter", "createHashRouter", "createMemoryRouter", "useRoutes", "createRoutesFromElements"].includes(node.expression.text)) {
      unsupported.push({ ...pos(sf, node), callee: node.expression.text, reason: "object/config route definitions are not analysed", raw: snippet(sf, node) });
    }
    node.forEachChild((c) => visit(c, parent));
  }
  visit(sf, null);
  return { routes, unsupported };
}

// ---------------------------------------------------------------- requests

function nameOfFunctionNode(fn) {
  if (ts.isFunctionDeclaration(fn) && fn.name) return fn.name.text;
  if (ts.isMethodDeclaration(fn) && fn.name) return fn.name.getText();
  const p = fn.parent;
  if (p && ts.isVariableDeclaration(p) && ts.isIdentifier(p.name)) return p.name.text;
  if (p && ts.isPropertyAssignment(p)) return p.name.getText();
  if (p && ts.isPropertyDeclaration(p)) return p.name.getText();
  if (p && ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken) return p.left.getText();
  return null;
}
const isFn = (n) => ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isArrowFunction(n) || ts.isMethodDeclaration(n) || ts.isConstructorDeclaration(n);

function enclosing(node) {
  const names = [];
  for (let n = node.parent; n; n = n.parent) {
    if (isFn(n)) {
      const nm = ts.isConstructorDeclaration(n) ? "constructor" : nameOfFunctionNode(n);
      if (nm) names.push(nm);
    }
  }
  return { inner: names[0] ?? "<module>", outer: names[names.length - 1] ?? "<module>" };
}

/** Find the declaration a bare identifier refers to: {param} | {init} | null. */
function lookup(ident) {
  const name = ident.text;
  for (let n = ident.parent; n; n = n.parent) {
    if (isFn(n)) {
      const idx = n.parameters.findIndex((p) => ts.isIdentifier(p.name) && p.name.text === name);
      if (idx >= 0) {
        const prm = n.parameters[idx];
        return { param: name, index: idx, optional: Boolean(prm.questionToken || prm.initializer), fn: n, fnName: nameOfFunctionNode(n) };
      }
    }
    if (ts.isBlock(n) || ts.isSourceFile(n) || ts.isModuleBlock(n)) {
      for (const st of n.statements) {
        if (ts.isVariableStatement(st) && (st.declarationList.flags & ts.NodeFlags.Const)) {
          for (const d of st.declarationList.declarations) {
            if (ts.isIdentifier(d.name) && d.name.text === name && d.initializer) return { init: d.initializer };
          }
        }
      }
    }
  }
  return null;
}

const lit = (v) => ({ t: "lit", v });
const dyn = (text) => ({ t: "dyn", text: text.replace(/\s+/g, "").slice(0, 48) });

/** URL expression -> parts [{t:lit|param|dyn}]. `consumed` collects apiUrl() call nodes folded into the URL. */
function resolveUrl(expr, ctx, depth = 0) {
  const sf = ctx.sf;
  expr = unwrap(expr);
  if (!expr) return [dyn("")];
  if (depth > 6) return [dyn(expr.getText(sf))];
  if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr)) return [lit(expr.text)];
  if (ts.isTemplateExpression(expr)) {
    const parts = [lit(expr.head.text)];
    for (const span of expr.templateSpans) {
      parts.push(...resolveUrl(span.expression, ctx, depth + 1));
      parts.push(lit(span.literal.text));
    }
    return parts;
  }
  if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    return [...resolveUrl(expr.left, ctx, depth + 1), ...resolveUrl(expr.right, ctx, depth + 1)];
  }
  if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken) {
    // `x ?? ""` : treat the left side as the value; an absent optional argument becomes the right literal.
    const left = resolveUrl(expr.left, ctx, depth + 1);
    return left.map((p) => (p.t === "param" ? { ...p, fallback: resolveUrl(expr.right, ctx, depth + 1) } : p));
  }
  if (ts.isCallExpression(expr) && ts.isIdentifier(expr.expression)) {
    const callee = expr.expression.text;
    if (callee === "apiUrl" && expr.arguments.length >= 1) {
      ctx.consumed.add(expr);
      return resolveUrl(expr.arguments[0], ctx, depth + 1);
    }
    if (URL_ENCODERS.has(callee) && expr.arguments.length === 1) return resolveUrl(expr.arguments[0], ctx, depth + 1);
  }
  if (ts.isNewExpression(expr) && ts.isIdentifier(expr.expression) && expr.expression.text === "URL" && expr.arguments?.length) {
    return resolveUrl(expr.arguments[0], ctx, depth + 1);
  }
  if (ts.isIdentifier(expr)) {
    const d = lookup(expr);
    if (d?.param) return [{ t: "param", name: d.param, index: d.index, optional: d.optional, fnName: d.fnName }];
    if (d?.init) return resolveUrl(d.init, ctx, depth + 1);
    return [dyn(expr.text)];
  }
  if (ts.isPropertyAccessExpression(expr) || ts.isElementAccessExpression(expr)) return [dyn(expr.getText(sf))];
  return [dyn(expr.getText(sf))];
}

function methodSpecOf(optExpr, ctx) {
  // Returns a spec describing how the HTTP method is determined for a request-making call.
  if (!optExpr) return { kind: "default", v: "GET" };
  const o = unwrap(optExpr);
  if (ts.isObjectLiteralExpression(o)) {
    let spread = false;
    for (const p of o.properties) {
      if (ts.isSpreadAssignment(p)) spread = true;
      if ((ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)) && p.name.getText() === "method") {
        if (ts.isShorthandPropertyAssignment(p)) {
          const d = lookup(p.name);
          return d?.param ? { kind: "param", name: d.param, index: d.index } : { kind: "dyn", text: "method" };
        }
        return methodExprSpec(p.initializer, ctx);
      }
    }
    return spread ? { kind: "dyn", text: "options contain spread, method not statically known" } : { kind: "default", v: "GET" };
  }
  if (ts.isIdentifier(o)) {
    const d = lookup(o);
    if (d?.param) return { kind: "param_prop", name: d.param, index: d.index, prop: "method", default: "GET" };
    if (d?.init) return methodSpecOf(d.init, ctx);
  }
  return { kind: "dyn", text: o.getText(ctx.sf).slice(0, 48) };
}
function methodExprSpec(e, ctx) {
  e = unwrap(e);
  if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return { kind: "literal", v: e.text.toUpperCase() };
  if (ts.isIdentifier(e)) {
    const d = lookup(e);
    if (d?.param) return { kind: "param", name: d.param, index: d.index };
    if (d?.init) return methodExprSpec(d.init, ctx);
  }
  if (ts.isBinaryExpression(e) && e.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken) {
    const left = unwrap(e.left);
    const right = methodExprSpec(e.right, ctx);
    if (ts.isPropertyAccessExpression(left) && ts.isIdentifier(left.expression)) {
      const d = lookup(left.expression);
      if (d?.param) return { kind: "param_prop", name: d.param, index: d.index, prop: left.name.text, fallback: right };
    }
    const l = methodExprSpec(left, ctx);
    return { ...l, fallback: right };
  }
  if (ts.isConditionalExpression(e)) {
    const a = methodExprSpec(e.whenTrue, ctx);
    const b = methodExprSpec(e.whenFalse, ctx);
    if (a.kind === "literal" && b.kind === "literal") return { kind: "choice", v: [a.v, b.v] };
  }
  return { kind: "dyn", text: e.getText(ctx.sf).slice(0, 48) };
}

function evalMethod(spec, args, ctx) {
  // -> {methods: string[] | null, basis}
  switch (spec.kind) {
    case "literal": return { methods: [spec.v], basis: "explicit_literal" };
    case "default": return { methods: [spec.v], basis: "default_get" };
    case "choice": return { methods: [...new Set(spec.v)], basis: "conditional_literals" };
    case "param": {
      const a = args[spec.index];
      if (a) {
        const u = unwrap(a);
        if (ts.isStringLiteral(u) || ts.isNoSubstitutionTemplateLiteral(u)) return { methods: [u.text.toUpperCase()], basis: "wrapper_param_literal" };
        return { methods: null, basis: "wrapper_param_dynamic" };
      }
      return spec.fallback ? evalMethod(spec.fallback, args, ctx) : { methods: null, basis: "wrapper_param_missing" };
    }
    case "param_prop": {
      const a = args[spec.index];
      if (!a) return spec.fallback ? evalMethod(spec.fallback, args, ctx) : { methods: [spec.default ?? "GET"], basis: "default_get" };
      const u = unwrap(a);
      if (ts.isObjectLiteralExpression(u)) {
        let spread = false;
        for (const p of u.properties) {
          if (ts.isSpreadAssignment(p)) spread = true;
          if ((ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)) && p.name.getText() === spec.prop) {
            if (ts.isPropertyAssignment(p)) {
              const v = unwrap(p.initializer);
              if (ts.isStringLiteral(v) || ts.isNoSubstitutionTemplateLiteral(v)) return { methods: [v.text.toUpperCase()], basis: "wrapper_options_literal" };
            }
            return { methods: null, basis: "wrapper_options_dynamic" };
          }
        }
        if (spread) return { methods: null, basis: "wrapper_options_spread" };
        return spec.fallback && spec.fallback.kind !== "dyn" ? evalMethod(spec.fallback, args, ctx) : { methods: [spec.default ?? "GET"], basis: "default_get" };
      }
      return { methods: null, basis: "wrapper_options_not_object_literal" };
    }
    default: return { methods: null, basis: "dynamic" };
  }
}

function stringify(parts) {
  return parts.map((p) => (p.t === "lit" ? p.v : p.t === "param" ? `\${${p.name}}` : `\${${p.text}}`)).join("");
}
function splitQuery(parts) {
  // cut at the first literal '?'; later parts only describe the query string
  const out = [];
  for (const p of parts) {
    if (p.t === "lit" && p.v.includes("?")) {
      out.push(lit(p.v.slice(0, p.v.indexOf("?"))));
      return { parts: out, query: true };
    }
    if (p.t === "dyn" && (p.text.includes("?") || p.text.includes("qs"))) {
      return { parts: out, query: true };
    }
    out.push(p);
  }
  return { parts: out, query: false };
}

function importBindings(sf) {
  const m = new Map(); // local name -> module specifier
  for (const st of sf.statements) {
    if (ts.isImportDeclaration(st) && st.importClause?.namedBindings && ts.isNamedImports(st.importClause.namedBindings)) {
      for (const el of st.importClause.namedBindings.elements) m.set(el.name.text, { spec: st.moduleSpecifier.text, imported: (el.propertyName ?? el.name).text });
    }
  }
  return m;
}

function collectCallSites(sf, filePath) {
  const ctx = { sf, consumed: new Set() };
  const sites = [];
  const apiUrlCalls = [];
  const bindings = importBindings(sf);
  function visit(n) {
    let callee = null;
    let kind = null;
    let urlExpr = null;
    let optExpr = null;
    let args = [];
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) {
      callee = n.expression.text;
      args = [...n.arguments];
      if (callee === "apiUrl") apiUrlCalls.push(n);
      else if (PRIMITIVE_CALLS[callee]) { kind = PRIMITIVE_CALLS[callee]; urlExpr = args[0]; optExpr = args[1]; }
      else { kind = "maybe_wrapper"; }
    } else if (ts.isNewExpression(n) && ts.isIdentifier(n.expression) && PRIMITIVE_NEW[n.expression.text]) {
      callee = `new ${n.expression.text}`;
      kind = PRIMITIVE_NEW[n.expression.text];
      args = [...(n.arguments ?? [])];
      urlExpr = args[0];
      optExpr = null;
    }
    if (kind) {
      const e = enclosing(n);
      sites.push({ node: n, callee, kind, args, urlExpr, optExpr, binding: bindings.get(callee) ?? null, fn: e.inner, topFn: e.outer, ...pos(sf, n) });
    }
    n.forEachChild(visit);
  }
  visit(sf);
  return { ctx, sites, apiUrlCalls, bindings };
}

function resolveModule(fromFile, spec, knownFiles) {
  if (!spec.startsWith(".")) return null;
  const base = path.posix.normalize(path.posix.join(path.posix.dirname(fromFile), spec));
  for (const cand of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) if (knownFiles.has(cand)) return cand;
  return null;
}

function analyzeApi(files, parsed) {
  const known = new Set(files.map((f) => f.path));
  const perFile = new Map();
  for (const f of files) {
    const p = parsed.get(f.path);
    if (!p || p.errors.length) continue;
    perFile.set(f.path, collectCallSites(p.sf, f.path));
  }

  // wrapper discovery to a fixed point: function whose wrapper-like parameter reaches a request URL
  const wrappers = new Map(); // key `${file}::${name}` -> def
  const defsByName = new Map();
  const addWrapper = (file, name, def) => {
    const key = `${file}::${name}`;
    if (wrappers.has(key)) return false;
    wrappers.set(key, { file, name, ...def });
    if (!defsByName.has(name)) defsByName.set(name, []);
    defsByName.get(name).push(wrappers.get(key));
    return true;
  };
  function findWrapperFor(file, site) {
    const b = site.binding;
    const local = wrappers.get(`${file}::${site.callee}`);
    if (local) return { def: local };
    if (b) {
      const target = resolveModule(file, b.spec, known);
      if (target) {
        const d = wrappers.get(`${target}::${b.imported}`);
        if (d) return { def: d };
      }
    }
    return { def: null };
  }

  for (let round = 0; round < 6; round++) {
    let changed = false;
    for (const [file, info] of perFile) {
      for (const s of info.sites) {
        let parts; let spec;
        if (s.kind === "maybe_wrapper") {
          const { def } = findWrapperFor(file, s);
          if (!def) continue;
          const urlArg = s.args[def.urlIndex];
          const subst = substituteParts(def, s, info.ctx);
          parts = subst.parts;
          spec = def.methodSpec;
          s._probeParts = parts;
        } else {
          parts = resolveUrl(s.urlExpr, info.ctx);
          spec = methodSpecOf(s.optExpr, info.ctx);
          s._probeParts = parts;
        }
        const lead = parts.find((p) => !(p.t === "lit" && p.v === ""));
        const ownerParams = parts.filter((p) => p.t === "param" && WRAPPER_PARAM_NAMES.has(p.name) && p.fnName);
        if (ownerParams.length) {
          const owner = ownerParams[0];
          const urlParts = parts;
          const changedNow = addWrapper(file, owner.fnName, { urlParts, urlIndex: owner.index, params: ownerParams.map((p) => p.name), methodSpec: spec, ownerKind: s.kind });
          changed = changed || changedNow;
        }
      }
    }
    if (!changed) break;
  }

  function substituteParts(def, site, ctx) {
    // map the wrapper's param placeholders onto the call-site arguments
    const out = [];
    let unresolved = false;
    for (const p of def.urlParts) {
      if (p.t === "param" && p.fnName === def.name) {
        const a = site.args[p.index];
        if (a) {
          const r = resolveUrl(a, ctx);
          out.push(...r);
        } else if (p.fallback) out.push(...p.fallback);
        else if (p.optional) out.push(lit(""));
        else { out.push(dyn(p.name)); unresolved = true; }
      } else out.push(p);
    }
    return { parts: out, unresolved };
  }

  const calls = []; const unresolved = []; const wrapperDefs = []; const nonApi = []; const urlRefs = [];
  for (const [file, info] of perFile) {
    for (const s of info.sites) {
      const base = { source_file: file, line: s.line, column: s.column, function: s.fn, top_level_function: s.topFn, callee: s.callee };
      let parts; let methodSpec; let callKind = s.kind; let args = s.args; let viaWrapper = null;
      if (s.kind === "maybe_wrapper") {
        const { def } = findWrapperFor(file, s);
        if (!def) {
          if (defsByName.has(s.callee) && !s.binding && !wrappers.has(`${file}::${s.callee}`)) {
            unresolved.push({ ...base, reason: "callee name equals a discovered request wrapper but the file neither defines nor imports it", expression: snippet(info.ctx.sf, s.node) });
          }
          continue;
        }
        viaWrapper = `${def.file}::${def.name}`;
        parts = substituteParts(def, s, info.ctx).parts;
        methodSpec = def.methodSpec;
        callKind = `wrapper:${def.name}`;
      } else {
        if (!s.urlExpr) { unresolved.push({ ...base, reason: "request call without url argument", expression: snippet(info.ctx.sf, s.node) }); continue; }
        parts = resolveUrl(s.urlExpr, info.ctx);
        methodSpec = s.kind === "websocket" ? { kind: "literal", v: "WS" } : methodSpecOf(s.optExpr, info.ctx);
      }
      const evalArgs = viaWrapper ? s.args : s.args.slice(1).length ? [undefined, ...s.args.slice(1)] : [];
      const mres = viaWrapper ? evalMethod(methodSpec, args, info.ctx) : (methodSpec.kind === "literal" || methodSpec.kind === "default" || methodSpec.kind === "choice" || methodSpec.kind === "dyn") ? evalMethod(methodSpec, args, info.ctx) : evalMethod(methodSpec, args, info.ctx);
      // wrapper definition: URL still contains wrapper-like params of the *enclosing* function
      const defParams = parts.filter((p) => p.t === "param" && WRAPPER_PARAM_NAMES.has(p.name) && wrappers.has(`${file}::${p.fnName}`));
      if (defParams.length) {
        wrapperDefs.push({ ...base, call_kind: callKind, url_template: stringify(parts), wrapper_function: defParams[0].fnName, url_param: defParams[0].name, method_spec: methodSpec.kind });
        continue;
      }
      const lead = parts.find((p) => !(p.t === "lit" && p.v === ""));
      const first = lead && lead.t === "lit" ? lead.v : null;
      const { parts: noQuery, query } = splitQuery(parts);
      const endpoint = stringify(noQuery);
      const raw = snippet(info.ctx.sf, s.node, 160);
      if (first !== null && /^(https?|wss?):\/\//.test(first)) {
        nonApi.push({ ...base, call_kind: callKind, url: endpoint, reason: "absolute URL not built through apiUrl()" });
        continue;
      }
      if (!lead || lead.t !== "lit" || !first.startsWith("/")) {
        unresolved.push({ ...base, call_kind: callKind, reason: lead && lead.t === "param" ? "url is a bare parameter of the enclosing function (not recognised as a request wrapper)" : "url not statically resolvable to a rooted path", url_template: endpoint, expression: raw });
        continue;
      }
      if (!mres.methods) {
        unresolved.push({ ...base, call_kind: callKind, reason: `http method not statically resolvable (${mres.basis})`, url_template: endpoint, expression: raw });
      }
      calls.push({
        ...base,
        call_kind: callKind,
        via_wrapper: viaWrapper,
        method: mres.methods ? mres.methods.join("|") : null,
        method_basis: mres.basis,
        endpoint,
        has_dynamic_segments: noQuery.some((p) => p.t !== "lit"),
        has_query: query || noQuery.length !== parts.length,
        raw,
      });
    }
    for (const a of info.apiUrlCalls) {
      if (info.ctx.consumed.has(a)) continue;
      const e = enclosing(a);
      const parts = resolveUrl(a.arguments[0], { sf: info.ctx.sf, consumed: new Set() });
      const { parts: noQuery } = splitQuery(parts);
      urlRefs.push({ source_file: file, ...pos(info.ctx.sf, a), function: e.inner, top_level_function: e.outer, url_template: stringify(noQuery), resolved: noQuery.length > 0 && noQuery[0].t === "lit" && noQuery[0].v.startsWith("/"), raw: snippet(info.ctx.sf, a, 160) });
    }
  }
  const key = (x) => `${x.source_file}:${String(x.line).padStart(6, "0")}:${String(x.column).padStart(4, "0")}`;
  for (const arr of [calls, unresolved, wrapperDefs, nonApi, urlRefs]) arr.sort((a, b) => key(a).localeCompare(key(b)));
  return {
    calls,
    unresolved,
    wrapper_definitions: wrapperDefs,
    non_api_requests: nonApi,
    url_references: urlRefs,
    wrappers: [...wrappers.values()].map((w) => ({ file: w.file, name: w.name, url_params: w.params, method_spec: w.methodSpec.kind, owner_kind: w.ownerKind })).sort((a, b) => `${a.file}${a.name}`.localeCompare(`${b.file}${b.name}`)),
  };
}

// ---------------------------------------------------------------- vitest call sites

function analyzeTests(sf) {
  const cases = []; const suites = [];
  const base = (n) => (ts.isIdentifier(n) ? n.text : ts.isPropertyAccessExpression(n) && ts.isIdentifier(n.expression) ? n.expression.text : null);
  const MOD = new Set(["skip", "only", "todo", "concurrent", "each", "fails", "runIf", "skipIf"]);
  function visit(n) {
    if (ts.isCallExpression(n)) {
      let callee = n.expression;
      let parameterized = false;
      let modifier = null;
      // it.each(table)("name", fn) -> CallExpression whose callee is a call of it.each
      if (ts.isCallExpression(callee) && ts.isPropertyAccessExpression(callee.expression) && callee.expression.name.text === "each") {
        parameterized = true; callee = callee.expression.expression;
      } else if (ts.isPropertyAccessExpression(callee) && MOD.has(callee.name.text)) {
        modifier = callee.name.text; callee = callee.expression;
        if (modifier === "each") return n.forEachChild(visit); // it.each(table) itself, counted at the outer call
      }
      const b = base(callee);
      if ((b === "it" || b === "test") && n.arguments.length >= 1 && (ts.isIdentifier(callee) || ts.isPropertyAccessExpression(callee))) {
        cases.push({ ...pos(sf, n), parameterized, modifier });
      } else if (b === "describe" && n.arguments.length >= 1) {
        suites.push({ ...pos(sf, n), modifier });
      }
    }
    n.forEachChild(visit);
  }
  visit(sf);
  return { cases, suites };
}

// ---------------------------------------------------------------- main

const input = JSON.parse(await readStdin());
const parsed = new Map();
const out = { parser: { name: "typescript", version: ts.version, dir: path.resolve(tsDir) }, limits: {
  wrapper_param_names: [...WRAPPER_PARAM_NAMES], route_wrapper_tags: [...ROUTE_WRAPPER_TAGS], primitives: { calls: Object.keys(PRIMITIVE_CALLS), new: Object.keys(PRIMITIVE_NEW) },
  not_supported: ["XMLHttpRequest/navigator.sendBeacon", "axios or other http libraries", "cross-module constant propagation", "object-config routers", "wrapper detection for functions whose URL params are not named in wrapper_param_names", "JS files outside the supplied list"],
}, files: {} };

for (const f of input.files) {
  const p = parse(f);
  parsed.set(f.path, p);
  out.files[f.path] = { parse_errors: p.errors };
  if (f.roles.includes("routes")) Object.assign(out.files[f.path], { routes: p.errors.length ? null : extractRoutes(p.sf) });
  if (f.roles.includes("tests")) Object.assign(out.files[f.path], { tests: p.errors.length ? null : analyzeTests(p.sf) });
}
const apiFiles = input.files.filter((f) => f.roles.includes("api"));
out.api = analyzeApi(apiFiles, parsed);
out.api_files = apiFiles.map((f) => f.path).sort();
process.stdout.write(JSON.stringify(out));

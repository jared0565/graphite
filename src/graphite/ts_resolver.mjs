#!/usr/bin/env node
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function normalizePath(value) {
  return value.replace(/\\/g, "/");
}

function loadTypeScript(root) {
  const candidates = [
    path.join(root, "package.json"),
    path.join(process.cwd(), "package.json"),
    path.join(path.dirname(new URL(import.meta.url).pathname), "package.json"),
  ];
  const errors = [];
  for (const candidate of candidates) {
    try {
      const req = createRequire(pathToFileURL(candidate));
      return { ts: req("typescript"), source: candidate };
    } catch (error) {
      errors.push(`${candidate}: ${error.message}`);
    }
  }
  return { ts: null, error: errors.join("\n") };
}

function parseConfig(ts, root, files) {
  const configPath = ts.findConfigFile(root, ts.sys.fileExists, "tsconfig.json");
  if (!configPath) {
    return {
      configPath: null,
      options: {
        allowJs: true,
        checkJs: false,
        moduleResolution: ts.ModuleResolutionKind.Node10,
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ESNext,
        noEmit: true,
        skipLibCheck: true,
      },
      fileNames: files,
    };
  }
  const read = ts.readConfigFile(configPath, ts.sys.readFile);
  if (read.error) {
    return {
      configPath,
      options: { noEmit: true, skipLibCheck: true },
      fileNames: files,
      configError: ts.flattenDiagnosticMessageText(read.error.messageText, "\n"),
    };
  }
  const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, path.dirname(configPath));
  return {
    configPath,
    options: { ...parsed.options, noEmit: true, skipLibCheck: true },
    fileNames: files,
    configError: parsed.errors?.length
      ? parsed.errors.map(e => ts.flattenDiagnosticMessageText(e.messageText, "\n")).join("\n")
      : null,
  };
}

function relFor(root, abs) {
  const rel = normalizePath(path.relative(root, abs));
  return rel && !rel.startsWith("../") && rel !== ".." ? rel : null;
}

function resolveModule(ts, host, options, root, knownFiles, containingFile, specifier) {
  const result = ts.resolveModuleName(specifier, containingFile, options, host).resolvedModule;
  if (!result || !result.resolvedFileName) return null;
  const resolved = normalizePath(result.resolvedFileName);
  if (result.isExternalLibraryImport || resolved.includes("/node_modules/")) return null;
  const rel = relFor(root, resolved);
  if (!rel) return null;
  const withoutDts = rel.replace(/\.d\.ts$/, ".ts");
  if (knownFiles.has(rel)) return rel;
  if (knownFiles.has(withoutDts)) return withoutDts;
  return null;
}

function lineOf(sourceFile, node) {
  return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
}

function moduleText(node) {
  return node && typeof node.text === "string" ? node.text : null;
}

function addUniqueEdge(edges, seen, edge) {
  const key = `${edge.source}\0${edge.target}\0${edge.relation}\0${edge.confidence}`;
  if (seen.has(key)) return;
  seen.add(key);
  edges.push(edge);
}

function collectModuleEdges(ts, program, host, options, root, knownFiles) {
  const edges = [];
  const seen = new Set();
  for (const sourceFile of program.getSourceFiles()) {
    const sourceRel = relFor(root, sourceFile.fileName);
    if (!sourceRel || !knownFiles.has(sourceRel)) continue;

    function addEdge(specifier, syntax, line, typeOnly = false) {
      const targetRel = resolveModule(ts, host, options, root, knownFiles, sourceFile.fileName, specifier);
      if (!targetRel || targetRel === sourceRel) return;
      const confidence = syntax === "export"
        ? "TS_COMPILER_EXPORT"
        : syntax === "dynamic_import"
          ? "TS_COMPILER_DYNAMIC_IMPORT"
          : typeOnly
            ? "TS_COMPILER_TYPE_IMPORT"
            : "TS_COMPILER_IMPORT";
      addUniqueEdge(edges, seen, {
        source: sourceRel,
        target: targetRel,
        specifier,
        syntax,
        relation: syntax === "export" ? "exports" : "imports",
        confidence,
        line,
      });
    }

    function visit(node) {
      if (ts.isImportDeclaration(node)) {
        const specifier = moduleText(node.moduleSpecifier);
        if (specifier) addEdge(specifier, "import", lineOf(sourceFile, node), Boolean(node.importClause?.isTypeOnly));
      } else if (ts.isExportDeclaration(node)) {
        const specifier = moduleText(node.moduleSpecifier);
        if (specifier) addEdge(specifier, "export", lineOf(sourceFile, node), Boolean(node.isTypeOnly));
      } else if (
        ts.isCallExpression(node)
        && node.expression.kind === ts.SyntaxKind.ImportKeyword
        && node.arguments.length === 1
        && ts.isStringLiteralLike(node.arguments[0])
      ) {
        addEdge(node.arguments[0].text, "dynamic_import", lineOf(sourceFile, node), false);
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  edges.sort(edgeSort);
  return edges;
}

function collectSymbolEdges(ts, program, root, knownFiles) {
  const checker = program.getTypeChecker();
  const edges = [];
  const seen = new Set();

  for (const sourceFile of program.getSourceFiles()) {
    const sourceRel = relFor(root, sourceFile.fileName);
    if (!sourceRel || !knownFiles.has(sourceRel)) continue;

    function visit(node) {
      if (ts.isIdentifier(node) && !isDeclarationOrImportName(ts, node)) {
        const edge = symbolEdgeForNode(ts, checker, root, knownFiles, sourceRel, sourceFile, node);
        if (edge) addUniqueEdge(edges, seen, edge);
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  edges.sort(edgeSort);
  return edges;
}

function symbolEdgeForNode(ts, checker, root, knownFiles, sourceRel, sourceFile, node) {
  let symbol = checker.getSymbolAtLocation(node);
  if (!symbol) return null;
  if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) {
    try {
      symbol = checker.getAliasedSymbol(symbol);
    } catch {
      return null;
    }
  }
  const declaration = firstProjectDeclaration(root, knownFiles, symbol.declarations || []);
  if (!declaration) return null;
  const targetRel = relFor(root, declaration.getSourceFile().fileName);
  if (!targetRel || targetRel === sourceRel || !knownFiles.has(targetRel)) return null;
  const typeOnly = isTypePosition(ts, node);
  return {
    source: sourceRel,
    target: targetRel,
    specifier: symbol.getName ? symbol.getName() : node.text,
    syntax: typeOnly ? "type_reference" : "symbol_reference",
    relation: typeOnly ? "type_references" : "references",
    confidence: typeOnly ? "TS_COMPILER_TYPE_REFERENCE" : "TS_COMPILER_SYMBOL_REFERENCE",
    line: lineOf(sourceFile, node),
  };
}

function firstProjectDeclaration(root, knownFiles, declarations) {
  for (const declaration of declarations) {
    const rel = relFor(root, declaration.getSourceFile().fileName);
    if (rel && knownFiles.has(rel)) return declaration;
  }
  return null;
}

function isDeclarationOrImportName(ts, node) {
  const parent = node.parent;
  if (!parent) return false;
  if (parent.name === node) {
    return ts.isClassDeclaration(parent)
      || ts.isFunctionDeclaration(parent)
      || ts.isVariableDeclaration(parent)
      || ts.isParameter(parent)
      || ts.isPropertyDeclaration(parent)
      || ts.isMethodDeclaration(parent)
      || ts.isInterfaceDeclaration(parent)
      || ts.isTypeAliasDeclaration(parent)
      || ts.isEnumDeclaration(parent)
      || ts.isImportClause(parent)
      || ts.isNamespaceImport(parent)
      || ts.isImportSpecifier(parent)
      || ts.isExportSpecifier(parent)
      || ts.isPropertySignature(parent)
      || ts.isMethodSignature(parent);
  }
  return ts.isImportSpecifier(parent)
    || ts.isExportSpecifier(parent)
    || ts.isImportClause(parent)
    || ts.isNamespaceImport(parent)
    || ts.isImportDeclaration(parent)
    || ts.isExportDeclaration(parent);
}

function isTypePosition(ts, node) {
  let current = node;
  while (current.parent) {
    const parent = current.parent;
    if (
      parent.kind === ts.SyntaxKind.TypeReference
      || parent.kind === ts.SyntaxKind.TypeQuery
      || parent.kind === ts.SyntaxKind.TypeLiteral
      || parent.kind === ts.SyntaxKind.TypeAliasDeclaration
      || parent.kind === ts.SyntaxKind.InterfaceDeclaration
      || parent.kind === ts.SyntaxKind.ExpressionWithTypeArguments
      || parent.kind === ts.SyntaxKind.HeritageClause
      || parent.type === current
      || parent.typeName === current
    ) {
      return true;
    }
    if (
      ts.isCallExpression(parent)
      || ts.isNewExpression(parent)
      || ts.isExpressionStatement(parent)
      || ts.isBinaryExpression(parent)
      || ts.isPropertyAccessExpression(parent)
    ) {
      return false;
    }
    current = parent;
  }
  return false;
}

function edgeSort(a, b) {
  return `${a.source}\0${a.relation}\0${a.confidence}\0${a.specifier}\0${a.target}`
    .localeCompare(`${b.source}\0${b.relation}\0${b.confidence}\0${b.specifier}\0${b.target}`);
}

try {
  const input = JSON.parse(await readStdin());
  const root = path.resolve(input.root);
  const relFiles = Array.isArray(input.files) ? input.files.map(normalizePath) : [];
  const absFiles = relFiles.map(file => path.resolve(root, file));
  const knownFiles = new Set(relFiles);
  const loaded = loadTypeScript(root);
  if (!loaded.ts) {
    console.log(JSON.stringify({ ok: false, reason: "typescript_not_available", detail: loaded.error }));
    process.exit(0);
  }
  const ts = loaded.ts;
  const parsed = parseConfig(ts, root, absFiles);
  const host = ts.createCompilerHost(parsed.options, true);
  const program = ts.createProgram(absFiles, parsed.options, host);
  const moduleEdges = collectModuleEdges(ts, program, host, parsed.options, root, knownFiles);
  const symbolEdges = input.symbolReferences === false ? [] : collectSymbolEdges(ts, program, root, knownFiles);
  const edges = [...moduleEdges, ...symbolEdges].sort(edgeSort);
  console.log(JSON.stringify({
    ok: true,
    typescriptVersion: ts.version,
    typescriptSource: loaded.source,
    configPath: parsed.configPath ? normalizePath(path.relative(root, parsed.configPath)) : null,
    configError: parsed.configError || null,
    fileCount: relFiles.length,
    moduleEdgeCount: moduleEdges.length,
    symbolEdgeCount: symbolEdges.length,
    edges,
  }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, reason: "resolver_error", detail: error?.stack || String(error) }));
  process.exit(0);
}




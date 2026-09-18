// Monaco 只在用户点开工作台后才进入浏览器。这里集中做三件事：
// 1. 只加载编辑器核心 + 计划允许的两种语言（Python/Markdown）；
// 2. 给 worker 接上 Vite 的模块 URL；
// 3. 不带 TS/JSON 语言服务：只读证据视图只需要语法高亮，不需要
//    语言服务猜类型，更不需要把几 MB 的 worker 拉进首开。
import type * as Monaco from "monaco-editor";

type MonacoModule = typeof Monaco;

let loading: Promise<MonacoModule> | null = null;

export function loadMonaco(): Promise<MonacoModule> {
  if (!loading) {
    loading = (async () => {
      // 只注册只读工作台真正使用的贡献点。不要引入 Monaco 的全量贡献点
      // 注册入口：它会把格式化、补全、重命名、TS/JSON
      // 语言服务等全部打进首开分块，远超工作台的 1.2MB gzip 预算。
      // editor.api 的类型是完整命名空间的子集（没有 css/html/lsp 等），
      // 运行时对象才是消费方真正用到的东西，这里经 unknown 收窄。
      const monaco = (await import(
        "monaco-editor/editor/editor.api")) as unknown as MonacoModule;
      await Promise.all([
        import("monaco-editor/features/codicon/register.js"),
        import("monaco-editor/features/find/register.js"),
        import("monaco-editor/features/folding/register.js"),
        import("monaco-editor/features/gotoLine/register.js"),
        import("monaco-editor/features/lineSelection/register.js"),
        import("monaco-editor/features/clipboard/register.js"),
        import("monaco-editor/features/contextmenu/register.js"),
        import("monaco-editor/features/hover/register.js"),
        import("monaco-editor/features/readOnlyMessage/register.js"),
        import("monaco-editor/features/bracketMatching/register.js"),
        import("monaco-editor/features/unicodeHighlighter/register.js"),
        import("monaco-editor/features/wordHighlighter/register.js"),
        import("monaco-editor/features/tokenization/register.js"),
      ]);
      await import("monaco-editor/languages/definitions/python/register.js");
      await import("monaco-editor/languages/definitions/markdown/register.js");
      const globalSelf = globalThis as unknown as {
        MonacoEnvironment?: {getWorker?: (workerId: string, label: string) => Worker};
      };
      globalSelf.MonacoEnvironment = {
        getWorker(_workerId: string, label: string) {
          return new Worker(new URL(
            "monaco-editor/editor/editor.worker.js", import.meta.url),
            {type: "module"});
        },
      };
      return monaco;
    })();
  }
  return loading;
}

/** 工作台文件模型的稳定身份：基础（只读）模型带 blob 指纹。 */
export function baseModelUri(reviewId: string, path: string,
                             blobSha256: string): string {
  return `review://${encodeURIComponent(reviewId)}/base/${path}?blob=${blobSha256}`;
}

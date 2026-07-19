// Bundled (by esbuild) into media/highlight.js and injected into the chat
// webview. Only the common languages, to keep it small. Exposes window.hljs;
// chat.html calls hljs.highlightElement on each <pre><code> after marked parses.
import hljs from 'highlight.js/lib/core';

import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cpp from 'highlight.js/lib/languages/cpp';
import csharp from 'highlight.js/lib/languages/csharp';
import css from 'highlight.js/lib/languages/css';
import diff from 'highlight.js/lib/languages/diff';
import go from 'highlight.js/lib/languages/go';
import ini from 'highlight.js/lib/languages/ini'; // toml-ish
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdown from 'highlight.js/lib/languages/markdown';
import python from 'highlight.js/lib/languages/python';
import ruby from 'highlight.js/lib/languages/ruby';
import rust from 'highlight.js/lib/languages/rust';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml'; // html
import yaml from 'highlight.js/lib/languages/yaml';

const langs: Record<string, unknown> = {
  bash, c, cpp, csharp, css, diff, go, ini, java, javascript, json, markdown,
  python, ruby, rust, shell, sql, typescript, xml, yaml,
};
for (const [name, lang] of Object.entries(langs)) {
  hljs.registerLanguage(name, lang as never);
}
// common aliases
hljs.registerAliases(['py'], { languageName: 'python' });
hljs.registerAliases(['js', 'jsx'], { languageName: 'javascript' });
hljs.registerAliases(['ts', 'tsx'], { languageName: 'typescript' });
hljs.registerAliases(['sh', 'zsh', 'console'], { languageName: 'shell' });
hljs.registerAliases(['yml'], { languageName: 'yaml' });
hljs.registerAliases(['html'], { languageName: 'xml' });
hljs.registerAliases(['toml'], { languageName: 'ini' });
hljs.registerAliases(['rs'], { languageName: 'rust' });
hljs.registerAliases(['cs'], { languageName: 'csharp' });

(globalThis as unknown as { hljs: typeof hljs }).hljs = hljs;

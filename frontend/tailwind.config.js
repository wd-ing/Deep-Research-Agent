/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 浅色为主（2026-10-11 定：不做深色）
        ink: '#1a1a1a',
        muted: '#6b7280',
        line: '#e5e7eb',
        /** 可点的东西：链接、焦点、主行动、选中竖条。
         *  ⚠️ **只归「可点」**，不再兼任「进行中」——那会让用户分不清
         *  「这是个能点的」和「这在动」。2026-10-14 定。 */
        accent: '#2563eb',
        /** 进行中。绿是因为它在语义上和「可点」无关，
         *  而 app 里同时只有一件事是绿的 → 看到绿就知道「这里还在跑」。 */
        live: '#16a34a',
      },
      // ⚠️ **只补档，不改默认档。** 改 `sm`/`base` 会让 31 处 `text-sm`
      //  集体变号，那不是「建立体系」而是「一次偷偷换字号」。
      //  现有 Tailwind 默认档 12/14/16/18/20 已经是一条像样的 scale，
      //  缺的只是最小的两档——原来那 56 处 `text-[Npx]` 就是因为缺档才现写的。
      //  收编后全项目只剩 11 / 12 / 14 / 16 / 18 / 20 六档。
      fontSize: {
        // 侧栏里的次要文字、计数、badge。以前写 `text-[11px]`（37 处）、
        // `text-[10px]`（13 处）、`text-[9px]`（2 处）三种，现在一档。
        // ⚠️ **键必须加引号**：`2xs` 以数字开头，不是合法的 JS 标识符。
        // 不加引号的话 Tailwind 用 sucrase 加载配置时会抛
        // 「Invalid or unexpected token」，而报错会被误指到 index.css 上——
        // 实测绕了一圈才定位到这里。
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
    },
  },
  plugins: [],
}

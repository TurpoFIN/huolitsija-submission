/** Huolitsija design system: Factory/Helios tokens (src/styles/tokens.css) mapped for Tailwind 3. */
const c = (v) => `color-mix(in oklch, var(${v}) calc(<alpha-value> * 100%), transparent)`;

module.exports = {
  darkMode: ["selector", '[data-theme="dark"]'],
  content: ["./src/**/*.{js,jsx,ts,tsx}", "./public/index.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["system-ui", "-apple-system", "BlinkMacSystemFont", '"Segoe UI"', "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "Consolas", '"Liberation Mono"', "monospace"],
      },
      fontSize: {
        xs: ["0.75rem", { lineHeight: "1rem" }],
        sm: ["0.8125rem", { lineHeight: "1.25rem" }],
        base: ["0.875rem", { lineHeight: "1.375rem" }],
        md: ["1rem", { lineHeight: "1.5rem" }],
        lg: ["1.125rem", { lineHeight: "1.625rem" }],
        xl: ["1.375rem", { lineHeight: "1.875rem" }],
        "2xl": ["1.75rem", { lineHeight: "2.25rem" }],
        "3xl": ["2.25rem", { lineHeight: "2.625rem" }],
        "4xl": ["3rem", { lineHeight: "1.1" }],
      },
      borderRadius: {
        sm: "calc(var(--radius) * 0.6)",
        md: "calc(var(--radius) * 0.8)",
        lg: "var(--radius)",
        xl: "calc(var(--radius) * 1.4)",
        "2xl": "calc(var(--radius) * 1.8)",
        "3xl": "calc(var(--radius) * 2.2)",
      },
      colors: {
        background: c("--background"),
        foreground: c("--foreground"),
        card: { DEFAULT: c("--card"), foreground: c("--card-foreground") },
        popover: { DEFAULT: c("--popover"), foreground: c("--popover-foreground") },
        primary: { DEFAULT: c("--primary"), foreground: c("--primary-foreground") },
        secondary: { DEFAULT: c("--secondary"), foreground: c("--secondary-foreground") },
        muted: { DEFAULT: c("--muted"), foreground: c("--muted-foreground") },
        accent: { DEFAULT: c("--accent"), foreground: c("--accent-foreground") },
        destructive: { DEFAULT: c("--destructive"), foreground: c("--on-accent") },
        border: c("--border"),
        input: c("--input"),
        ring: c("--ring"),
        surface: c("--bg-surface"),
        canvas: c("--bg-canvas"),
        subtle: c("--bg-subtle"),
        hover: "var(--bg-hover)",
        ink: {
          DEFAULT: c("--text-primary"),
          secondary: c("--text-secondary"),
          tertiary: c("--text-tertiary"),
          disabled: c("--text-disabled"),
          accent: c("--text-accent"),
        },
        brand: { DEFAULT: c("--accent-solid"), foreground: c("--on-accent") },
        success: { DEFAULT: c("--success-500"), fg: c("--text-success") },
        danger: { DEFAULT: c("--danger-500"), fg: c("--text-danger") },
        warning: { DEFAULT: c("--warning-500"), fg: c("--text-warning") },
        info: { DEFAULT: c("--info-500"), fg: c("--text-info") },
        chart: {
          1: c("--chart-1"), 2: c("--chart-2"), 3: c("--chart-3"), 4: c("--chart-4"),
          5: c("--chart-5"), 6: c("--chart-6"), 7: c("--chart-7"),
        },
        sidebar: {
          DEFAULT: c("--sidebar"), foreground: c("--sidebar-foreground"), accent: "var(--sidebar-accent)",
          border: c("--sidebar-border"),
        },
      },
      transitionDuration: { DEFAULT: "180ms", fast: "120ms", slow: "260ms" },
      transitionTimingFunction: { DEFAULT: "cubic-bezier(0.22, 1, 0.36, 1)" },
      keyframes: {
        "accordion-down": { from: { height: "0" }, to: { height: "var(--radix-accordion-content-height)" } },
        "accordion-up": { from: { height: "var(--radix-accordion-content-height)" }, to: { height: "0" } },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

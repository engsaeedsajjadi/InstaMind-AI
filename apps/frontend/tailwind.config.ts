import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Vazirmatn", "Tahoma", "Segoe UI", "sans-serif"],
      },
      colors: {
        brand: {
          50: "#eef5ff",
          100: "#d9e8ff",
          200: "#bcd8ff",
          300: "#8ebfff",
          400: "#599bff",
          500: "#3376ff",
          600: "#1b54f5",
          700: "#143fe1",
          800: "#1734b6",
          900: "#19308f",
        },
      },
    },
  },
  plugins: [],
} satisfies Config;

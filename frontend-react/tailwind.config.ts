import type { Config } from 'tailwindcss'
import typography from '@tailwindcss/typography'

const config: Config = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'Noto Sans SC', 'system-ui', 'sans-serif'],
        'body-cjk': ['Noto Sans SC', 'Inter', 'system-ui', 'sans-serif'],
        // DESIGN.md §8.7 D1: 阅读衬线走 Noto Serif SC 单字体方案(自托管,内置拉丁专为配汉字设计)。
        // 中文名 Source Han Serif SC / 宋体只作字体加载失败时的系统兜底;不再前置独立西文衬线。
        display: ['Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'STSong', 'Georgia', 'serif'],
        'event-title': ['Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'STSong', 'Georgia', 'serif'],
        brand: ['Cormorant Garamond', 'Georgia', 'serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      colors: {
        // Warm neutral system
        warm: {
          50:  '#FAFAF7',
          100: '#F5F4F0',
          200: '#F2F1ED',
          300: '#E4E2DC',
          400: '#D0CEC7',
          500: '#ADACA6',
          600: '#7A7974',
          700: '#4A4946',
          800: '#1A1917',
          900: '#0F0F0E',
        },
        // Brand indigo
        indigo: {
          50:  '#EEEDFF',
          400: '#818CF8',
          500: '#6366F1',
          600: '#4F52E4',
        },
        // Semantic: amber (AI/suggestions/high score)
        amber: {
          DEFAULT: '#D97706',
          bg: '#FFFBEB',
          border: '#FDE68A',
          dark: '#FBBF24',
          'dark-bg': '#2A2520',
          'dark-border': '#5C4A1E',
        },
        // Semantic: emerald (actions/success)
        emerald: {
          DEFAULT: '#059669',
          bg: '#ECFDF5',
          border: '#A7F3D0',
          dark: '#34D399',
          'dark-bg': '#1A2A22',
          'dark-border': '#1E5C3A',
        },
        // Semantic: sky (AI insight)
        sky: {
          DEFAULT: '#0284C7',
          bg: '#F0F9FF',
          border: '#BAE6FD',
          dark: '#38BDF8',
          'dark-bg': '#1A2530',
          'dark-border': '#1E4A5C',
        },
        // Platform colors
        platform: {
          twitter: '#0F1419',
          xhs: '#FE2C55',
          bili: '#00A1D6',
          reddit: '#FF4500',
          hn: '#FF6600',
          github: '#333333',
          youtube: '#FF0000',
          rss: '#F26522',
          lingowhale: '#2DB84B',
          waytoagi: '#4E6EF2',
        },
        // Semantic tokens (CSS variable driven)
        background: 'var(--background)',
        foreground: 'var(--foreground)',
        card: { DEFAULT: 'var(--card)', foreground: 'var(--card-foreground)' },
        primary: { DEFAULT: 'var(--primary)', foreground: 'var(--primary-foreground)' },
        secondary: { DEFAULT: 'var(--secondary)', foreground: 'var(--secondary-foreground)' },
        muted: { DEFAULT: 'var(--muted)', foreground: 'var(--muted-foreground)' },
        accent: { DEFAULT: 'var(--accent)', foreground: 'var(--accent-foreground)' },
        destructive: { DEFAULT: 'var(--destructive)', foreground: 'var(--destructive-foreground)' },
        border: 'var(--border)',
        input: 'var(--input)',
        ring: 'var(--ring)',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      boxShadow: {
        subtle: '0 1px 3px rgba(0,0,0,.04)',
        medium: '0 4px 16px rgba(0,0,0,.07)',
        prominent: '0 12px 40px rgba(0,0,0,.1)',
      },
      spacing: {
        '1': '4px',
        '2': '8px',
        '3': '12px',
        '4': '16px',
        '5': '20px',
        '6': '24px',
        '8': '32px',
        '10': '40px',
        '12': '48px',
      },
      fontSize: {
        'xs': ['12px', { lineHeight: '1.5' }],
        'sm': ['14px', { lineHeight: '1.5' }],
        'base': ['16px', { lineHeight: '1.6' }],
        'lg': ['18px', { lineHeight: '1.4' }],
        'xl': ['20px', { lineHeight: '1.4' }],
        'h3': ['18px', { lineHeight: '1.3', fontWeight: '600' }],
        'h2': ['22px', { lineHeight: '1.3', fontWeight: '700' }],
        'h1': ['17px', { lineHeight: '1.3', fontWeight: '800' }],
      },
      keyframes: {
        'blur-fade': {
          from: { opacity: '0', transform: 'translateY(12px)', filter: 'blur(6px)' },
          to: { opacity: '1', transform: 'translateY(0)', filter: 'blur(0)' },
        },
        'zone-in': {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'shimmer': {
          from: { backgroundPosition: '200% 0' },
          to: { backgroundPosition: '-200% 0' },
        },
        'spin': {
          from: { transform: 'rotate(0deg)' },
          to: { transform: 'rotate(360deg)' },
        },
        'skeleton': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        'indeterminate-bar': {
          '0%': { left: '-35%' },
          '100%': { left: '100%' },
        },
      },
      animation: {
        'blur-fade': 'blur-fade 200ms ease-out both',
        'zone-in': 'zone-in 300ms ease-out',
        'shimmer': 'shimmer 3s infinite',
        'shimmer-fast': 'shimmer 1.5s infinite',
        'spin': 'spin 1s linear infinite',
        'spin-fast': 'spin 0.8s linear infinite',
        'skeleton': 'skeleton 1.5s linear infinite',
        'indeterminate-bar': 'indeterminate-bar 1.4s ease-in-out infinite',
      },
    },
  },
  plugins: [
    typography,
  ],
}

export default config

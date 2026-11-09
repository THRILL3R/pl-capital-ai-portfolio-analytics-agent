import type { Config } from 'tailwindcss';

/**
 * Design tokens from the Stitch export (design-reference/pl_capital_portfolio_analytics/DESIGN.md),
 * with ONE deliberate change: the typeface is Figtree (PL Capital's real brand font, measured on
 * plindia.com), not Hanken Grotesk.
 *
 * Note: the Stitch HTML applied `font-*` classes (font-family only) where the type scale was
 * intended. Here the scale is exposed as `text-*` utilities (size + line-height + weight +
 * tracking), which is how DESIGN.md defines it.
 */
const figtree = ['"Figtree Variable"', 'Figtree', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'Arial', 'sans-serif'];

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: '#3330b4',
        'on-primary': '#ffffff',
        'primary-container': '#4c4ccc',
        'on-primary-container': '#d5d3ff',
        secondary: '#5455a9',
        tertiary: '#2a31b7',
        'tertiary-fixed': '#e0e0ff',
        'primary-fixed': '#e1dfff',
        error: '#ba1a1a',
        background: '#f7f9ff',
        surface: '#f7f9ff',
        'surface-base': '#FFFFFF',
        'surface-container-lowest': '#ffffff',
        'surface-container-low': '#f1f4f9',
        'surface-container': '#ebeef3',
        'surface-container-high': '#e5e8ee',
        'surface-container-highest': '#e0e3e8',
        'surface-table-header': '#F7F7F7',
        'brand-blue': '#0000A0',
        'brand-green': '#1FB15A',
        'primary-hover': '#3636BC',
        'violet-accent': '#3922D1',
        'indigo-950': '#001A66',
        'indigo-900': '#07075A',
        'indigo-850': '#0E0E6A',
        'indigo-800': '#0F0F83',
        'indigo-750': '#111C74',
        'indigo-700': '#272756',
        'indigo-650': '#2C2C89',
        'lavender-light': '#D0D0FC',
        'lavender-input': '#E8E8FF',
        'lavender-callout': '#F5F5FF',
        'text-primary': '#212529',
        'text-secondary': '#555555',
        'text-muted': '#6B7280',
        'border-hairline': '#E2E8F0',
        'border-table-row': '#F0F0F0',
        'status-positive-text': '#22C55E',
        'status-positive-bg': '#D7FFE5',
        'status-negative-text': '#E53935',
        'status-negative-bg': '#FDECEA',
        'chart-series-1': '#0000A0',
        'chart-series-2': '#4C4CCC',
        'chart-series-3': '#7D7DD0',
        'chart-series-4': '#B6B6F7',
        'chart-series-5': '#3922D1',
        'chart-series-6': '#272756',
        'chart-series-7': '#6B7280',
        'chart-series-8': '#D0D0FC',
      },
      borderRadius: { DEFAULT: '0.25rem', lg: '0.5rem', xl: '0.75rem', full: '9999px' },
      spacing: { gutter: '1rem', 'gutter-lg': '1.5rem', margin: '1rem', 'margin-md': '1.5rem', 'margin-lg': '2rem' },
      fontFamily: { sans: figtree },
      fontSize: {
        'display-hero': ['40px', { lineHeight: '48px', letterSpacing: '-0.02em', fontWeight: '800' }],
        'display-hero-mobile': ['30px', { lineHeight: '38px', letterSpacing: '-0.015em', fontWeight: '800' }],
        'headline-kpi': ['32px', { lineHeight: '38px', letterSpacing: '-0.01em', fontWeight: '700' }],
        'headline-kpi-mobile': ['26px', { lineHeight: '32px', letterSpacing: '-0.01em', fontWeight: '700' }],
        'headline-lg': ['24px', { lineHeight: '32px', letterSpacing: '-0.01em', fontWeight: '700' }],
        'headline-md': ['20px', { lineHeight: '28px', fontWeight: '600' }],
        'headline-sm': ['16px', { lineHeight: '24px', fontWeight: '600' }],
        'body-lg': ['16px', { lineHeight: '24px', fontWeight: '400' }],
        'body-md': ['14px', { lineHeight: '20px', fontWeight: '400' }],
        'body-sm': ['12px', { lineHeight: '16px', fontWeight: '400' }],
        'label-lg': ['14px', { lineHeight: '20px', letterSpacing: '0.01em', fontWeight: '600' }],
        'label-md': ['12px', { lineHeight: '16px', letterSpacing: '0.02em', fontWeight: '600' }],
        'label-sm': ['11px', { lineHeight: '14px', letterSpacing: '0.03em', fontWeight: '500' }],
        'table-cell': ['13px', { lineHeight: '18px', fontWeight: '400' }],
        'table-header': ['11px', { lineHeight: '16px', letterSpacing: '0.05em', fontWeight: '700' }],
      },
      boxShadow: {
        ambient: '5px 3px 12px rgba(39, 39, 86, 0.10)',
        offset: '4px 4px 0px #3922D1',
      },
    },
  },
  plugins: [],
} satisfies Config;

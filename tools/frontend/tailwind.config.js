/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    '../../templates/**/*.html',
    '../build_static_site.py',
  ],
  theme: {
    extend: {
      colors: {
        brand: {50:'#ecfdf5',100:'#d1fae5',500:'#10b981',600:'#059669',700:'#047857'},
        accentlime: '#84cc16'
      },
      fontFamily: {
        sans: ['Inter','"Noto Sans JP"','Hiragino Kaku Gothic ProN','Yu Gothic','sans-serif']
      },
      boxShadow: {
        card: '0 2px 12px rgba(4,120,87,0.08)',
        cardhover: '0 10px 28px rgba(4,120,87,0.16)'
      }
    }
  },
  plugins: [require('daisyui')],
}

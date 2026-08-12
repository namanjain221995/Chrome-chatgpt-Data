/** ESLint 8 flat-free config for the MV3 extension. */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, webextensions: true, node: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['@typescript-eslint'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
  ignorePatterns: ['dist/', 'node_modules/', '*.config.ts', 'scripts/*.mjs'],
  rules: {
    '@typescript-eslint/no-explicit-any': 'error',
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    '@typescript-eslint/explicit-function-return-type': 'off',
    'no-console': ['error', { allow: ['info', 'warn', 'error'] }],
    'no-eval': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',
    eqeqeq: ['error', 'smart'],
    'prefer-const': 'error',
    // The extension must never touch page storage or cookies.
    'no-restricted-globals': [
      'error',
      { name: 'localStorage', message: 'Use chrome.storage; page storage is off limits.' },
      { name: 'sessionStorage', message: 'Use chrome.storage; page storage is off limits.' },
    ],
    'no-restricted-properties': [
      'error',
      { object: 'document', property: 'cookie', message: 'Cookies are never read by this extension.' },
      { object: 'element', property: 'innerHTML', message: 'Never assign page HTML; use textContent.' },
    ],
  },
  overrides: [
    {
      files: ['tests/**/*.ts'],
      rules: { '@typescript-eslint/no-explicit-any': 'off', 'no-console': 'off' },
    },
  ],
};

# gh0st browser dashboard

This Next.js app provides the shadcn preset-based browser interface for the local gh0st API.

## Development

```bash
npm ci
npm run dev
```

The development server serves the UI on port 3000. Run `gh0st api` separately and use the local API in the browser.

## Static package build

```bash
npm run build:local
```

This creates a static Next.js export and copies it into `gh0st/static/web`, where the Python API serves it at `http://127.0.0.1:8383/ui`. Keep the generated export checked in so source installs and Python wheels include the same UI. The release workflow rebuilds the export before testing and packaging.

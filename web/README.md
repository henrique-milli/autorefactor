# Autorefactor report UI

Next.js viewer for engine runs: ROI, map, queue, jobs, Settings. It does **not** start analyze — agents do that via MCP.

## Run it

Engine must be up on `:8765` (from the product repo root):

```bash
AUTOREFACTOR_MCP_TRANSPORT=streamable-http uv run autorefactor-mcp
```

Then:

```bash
cd web
npm install
AUTOREFACTOR_API_URL=http://127.0.0.1:8765 npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Settings live at `/settings`. `/runs` stays empty until an agent analyzes a workspace.

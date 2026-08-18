# Autorefactor report UI

Next.js viewer for engine runs: ROI, map, queue, jobs, Settings. It does **not** start analyze — agents do that via MCP.

## With Compose (usual)

From the product repo root:

```bash
docker compose up -d --build
```

Open [http://localhost:3000](http://localhost:3000). Settings live at `/settings`. `/runs` stays empty until an agent analyzes a workspace.

## Local Next without the web image

Engine must already be up (Compose `autorefactor` or `uv run autorefactor-mcp` on `:8765`):

```bash
cd web
npm install
AUTOREFACTOR_API_URL=http://127.0.0.1:8765 npm run dev
```

Then open [http://localhost:3000](http://localhost:3000).

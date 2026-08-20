# Router node (mesh tier 2)

4th node for the tri-nodal mesh: a 1.5B always-on model that decides which node
handles each request, then dispatches it onto the mesh bus from tier 1.

Install (Termux, Pixel 8 Pro):
    tar xzf mesh_router.tar.gz && cd mesh_router && bash router_up.sh

Requires tier 1 (mesh_bus.py / `mesh` CLI) for dispatch; classification works
standalone. If ollama or the model is missing it falls back to a deterministic
keyword scorer, so routing never stops.

Commands: route "..." | route --explain --dry-run "..." | route --daemon | route --selftest
Env: MESH_ROUTER_MODEL (default qwen2.5:1.5b), OLLAMA_HOST

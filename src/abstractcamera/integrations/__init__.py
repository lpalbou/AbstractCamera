"""Host-framework integrations.

`abstractcore_plugin` registers abstractcamera as an AbstractCore capability
plugin (entry point group `abstractcore.capabilities_plugins`);
`abstractcore_tools` exposes the camera tool set for LLM tool calling.
Both delegate to `abstractcamera.service` — one implementation, two surfaces.
"""

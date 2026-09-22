"""Blueprint Phase 8: local, deterministic document artifacts.

A validated Phase 4 preparation (ordered evidence-backed blocks) is rendered
into real PDF / DOCX files on the local filesystem, with database metadata
(version, SHA-256, input fingerprint, renderer version) that ties every file
back to the exact preparation and evidence snapshot that produced it.

Rendering is layout only: it never rewrites, adds or drops candidate content,
and it never calls an AI.
"""

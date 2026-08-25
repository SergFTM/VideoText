"""Shared core for all assistant personas.

Modules here are persona-agnostic: chat loop, provider adapters, cache,
context builder, knowledge base, tools runtime. Personas supply data
(prompts, tool lists, KB sources) — core supplies logic.
"""

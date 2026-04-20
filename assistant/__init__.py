"""AI operational assistant for VideoText.

Subsystems:
- knowledge_base: loads errors.yaml + kb_static.md + AST-scan of codebase into AssistantKB
- context_builder: cherry-picks relevant KB + live DB state for a given user question
- analyzer: TF-IDF + scoring + dedup primitives (no AI calls)
- cache: fastembed-backed fuzzy Q&A cache (reuses prior answers for similar questions)
- tools: callable functions the model invokes via function-calling (test/save/enrich/...)
- chat: provider-agnostic chat loop with SSE streaming and tool-use

Design: reads heavy, writes careful. Every write tool requires `confirm=True`.
"""

# Lazy re-export — chat depends on providers that may be optional.
# Import directly from submodules: `from assistant.chat import Assistant`.
__all__: list[str] = []

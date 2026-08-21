#!/usr/bin/env python3
"""One-shot exact-source repair for the Phase-5 integration authority."""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(sys.argv[1])
TARGET = Path(sys.argv[2])

old = '''        from firecrawl_skill.research_store.orchestrator import (
            OrchestratorConfig,
            ResearchOrchestrator,
        )
        from firecrawl_skill.research_store.run_service import ResearchRunService

        # Build orchestrator
        orchestrator = ResearchOrchestrator.build(
            config=service.config,
            orchestrator_config=OrchestratorConfig(max_adaptive_cycles=2),
        )
'''
new = '''        from firecrawl_skill.research_store.composition import (
            build_orchestrator_instance,
        )
        from firecrawl_skill.research_store.orchestrator import (
            OrchestratorConfig,
            ResearchOrchestrator,
        )
        from firecrawl_skill.research_store.run_service import ResearchRunService

        # Build the base application orchestrator through canonical composition.
        orchestrator = build_orchestrator_instance(
            ResearchOrchestrator,
            service.config,
            orchestrator_config=OrchestratorConfig(max_adaptive_cycles=2),
        )
'''

source = SOURCE.read_text(encoding="utf-8")
count = source.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one stale builder block, found {count}")
repaired = source.replace(old, new, 1)
if "ResearchOrchestrator.build(" in repaired:
    raise SystemExit("stale ResearchOrchestrator.build caller remains")
TARGET.write_text(repaired, encoding="utf-8")

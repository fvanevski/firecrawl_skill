from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

COMMANDS = {
    "export-invocation",
    "packet-validate",
    "packet-inspect",
    "packet-diff",
    "packet-export",
    "handoff",
    "claim-manifest",
}


def run(args, config, deps):
    command = args.command
    if command == "export-invocation":
        from ..evidence_admin import export_invocation

        result = export_invocation(
            config, args.invocation_id, uow_factory=deps._uow_factory
        )
        deps._export_json(Path(args.output), result)
        print(deps.dumps(result))
        return 0
    if command == "packet-validate":
        from firecrawl_skill.research_domain.registry import load_model

        from ..container import build_evidence_service
        from ..packet_validator import EvidencePacketValidator

        packet_rec = build_evidence_service(config).export_packet(
            UUID(args.run_id), args.revision
        )
        if packet_rec is None:
            raise SystemExit(
                f"evidence packet not found for run {args.run_id}"
                + (f" r{args.revision}" if args.revision else "")
            )
        vr = EvidencePacketValidator().validate(load_model(packet_rec))
        if args.output == "-":
            if vr.is_valid and vr.is_complete:
                print(vr.to_json(indent=2))
                return 0
            import sys

            print(vr.to_json(indent=2), file=sys.stderr)
            if vr.errors:
                return 1
            if not args.include_warnings and vr.warnings:
                return 1
            return 0
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                file.write(vr.to_json(indent=2))
            os.replace(tmp_path, str(output_path))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return None
    if command == "packet-inspect":
        from firecrawl_skill.research_domain.registry import load_model

        from ..container import build_evidence_service
        from ..packet_validator import (
            EvidencePacketValidator,
            bounded_citation_ready_output,
        )

        packet_rec = build_evidence_service(config).export_packet(
            UUID(args.run_id), args.revision
        )
        if packet_rec is None:
            raise SystemExit(
                f"evidence packet not found for run {args.run_id}"
                + (f" r{args.revision}" if args.revision else "")
            )
        packet = load_model(packet_rec)
        validation = EvidencePacketValidator().validate(packet)
        if args.bounded:
            output_dict = bounded_citation_ready_output(
                packet, max_passages=args.max_passages, max_claims=args.max_claims
            )
        else:
            output_dict = packet_rec.to_dict()
            output_dict["validation"] = validation.to_dict()
        if args.output == "-":
            print(json.dumps(output_dict, indent=2, default=str))
            return 0
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                file.write(json.dumps(output_dict, indent=2, default=str))
            os.replace(tmp_path, str(output_path))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return None
    if command == "packet-diff":
        from firecrawl_skill.research_domain.registry import load_model

        from ..container import build_evidence_service
        from ..packet_diff import diff_packets

        evidence_svc = build_evidence_service(config)
        run_id = UUID(args.run_id)
        old_rec = evidence_svc.export_packet(run_id, args.old_revision)
        if old_rec is None:
            raise SystemExit(
                f"evidence packet not found for run {args.run_id} r{args.old_revision}"
            )
        new_rec = evidence_svc.export_packet(run_id, args.new_revision)
        if new_rec is None:
            raise SystemExit(
                f"evidence packet not found for run {args.run_id} r{args.new_revision}"
            )
        diff = diff_packets(
            load_model(old_rec),
            load_model(new_rec),
            old_revision=args.old_revision,
            new_revision=args.new_revision,
        )
        if args.output == "-":
            print(diff.to_json(indent=2))
            return 0
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                file.write(diff.to_json(indent=2))
            os.replace(tmp_path, str(output_path))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return None
    if command == "packet-export":
        from ..container import build_evidence_service
        from ..packet_validator import bounded_citation_ready_output

        packet_rec = build_evidence_service(config).export_packet(
            UUID(args.run_id), args.revision
        )
        if packet_rec is None:
            raise SystemExit(
                f"evidence packet not found for run {args.run_id}"
                + (f" r{args.revision}" if args.revision else "")
            )
        if args.bounded:
            from firecrawl_skill.research_domain.registry import load_model

            output_dict = bounded_citation_ready_output(
                load_model(packet_rec),
                max_passages=args.max_passages,
                max_claims=args.max_claims,
            )
        else:
            output_dict = packet_rec.to_dict()
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file:
                file.write(json.dumps(output_dict, indent=2, default=str))
            os.replace(tmp_path, str(output_path))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return None
    if command == "handoff":
        from ..handoff_admin import build_handoff

        output_dict = build_handoff(config, args, uow_type=deps.PostgresUnitOfWork)
        if args.output != "-":
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmpfile = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as file:
                    file.write(json.dumps(output_dict, indent=2, default=str))
                os.replace(tmpfile, str(output_path))
                return {"exported_to": str(output_path)}
            except BaseException:
                os.unlink(tmpfile)
                raise
        print(json.dumps(output_dict, indent=2, default=str))
        return {}
    if command == "claim-manifest":
        from ..container import build_claim_service

        claim_svc = build_claim_service(config)
        if args.claim_command == "import":
            run_id = deps._resolve_run_id(config, args.external_id)
            manifest_path = Path(args.file)
            if not manifest_path.is_file():
                raise SystemExit(f"manifest file not found: {args.file}")
            with open(manifest_path, "r") as file:
                manifest = json.load(file)
            claim_svc.import_manifest(
                run_id, manifest, dry_run=getattr(args, "dry_run", False)
            )
            return None
        if args.claim_command == "export":
            run_id = deps._resolve_any_run_id(config, args.external_id)
            manifest = claim_svc.export_manifest(run_id)
            if args.output == "-":
                print(deps.dumps(manifest))
                return 0
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as file:
                    file.write(deps.dumps(manifest))
                os.replace(tmp_path, str(output_path))
            except BaseException:
                os.unlink(tmp_path)
                raise
            return None
        if args.claim_command == "list":
            run_id = deps._resolve_any_run_id(config, args.external_id)
            claim_svc.list_claims(run_id)
            claim_svc.list_evidence_links(run_id)
            return None
        raise SystemExit(f"unknown claim-manifest command: {args.claim_command}")
    raise AssertionError(command)

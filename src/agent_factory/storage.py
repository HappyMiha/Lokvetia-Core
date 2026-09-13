from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .lifecycle import TRANSITIONS, ensure_transition
from .models import AssignmentLease, Budget, Status, WorkItem
from .game_feedback import FEEDBACK_MIGRATION
from .playable_versions import PLAYABLE_VERSION_MIGRATION
from .studio_autonomy import AUTONOMY_MIGRATION
from .studio_cost import COST_MIGRATION
from .studio_paid_tools import PAID_TOOL_MIGRATION
from .studio_roster import ROSTER_MIGRATION
from .studio_first_run import FIRST_RUN_MIGRATION
from .studio_supervisor import SUPERVISOR_MIGRATION
from .studio_workers import WORKER_MIGRATION
from .studio_cycles import CYCLE_MIGRATION
from .studio_slices import SLICE_MIGRATION
from .settings_store import SETTINGS_MIGRATION
from .worker_admission import ADMISSION_MIGRATION

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS work_items(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), title TEXT NOT NULL, description TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS workflow_runs(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), task_id INTEGER NOT NULL REFERENCES work_items(id), workflow_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES workflow_runs(id), stage TEXT NOT NULL, agent_id TEXT NOT NULL, provider TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', review_note TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS approval_gates(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id), status TEXT NOT NULL DEFAULT 'pending', decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, decided_at TEXT);
        CREATE TABLE IF NOT EXISTS provider_execution_gates(id INTEGER PRIMARY KEY, provider TEXT NOT NULL, agent_id TEXT NOT NULL, task_id INTEGER NOT NULL REFERENCES work_items(id), status TEXT NOT NULL DEFAULT 'pending', decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, decided_at TEXT, consumed_at TEXT);
        CREATE TABLE IF NOT EXISTS provider_execution_artifacts(id INTEGER PRIMARY KEY, gate_id INTEGER NOT NULL UNIQUE REFERENCES provider_execution_gates(id), provider TEXT NOT NULL, agent_id TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    """),
    (2, """
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_task ON workflow_runs(task_id, workflow_id, status);
        CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id, id);
        UPDATE workflow_runs
           SET status='awaiting_approval', completed_at=NULL
         WHERE status='completed'
           AND EXISTS (SELECT 1 FROM approval_gates g WHERE g.run_id=workflow_runs.id AND g.status='pending');
    """),
    (3, """
        CREATE TABLE IF NOT EXISTS provider_execution_attempts(
            id INTEGER PRIMARY KEY,
            gate_id INTEGER NOT NULL UNIQUE REFERENCES provider_execution_gates(id),
            provider TEXT NOT NULL, agent_id TEXT NOT NULL,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            request_hash TEXT NOT NULL, definition_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('claimed','running','succeeded','failed','abandoned')),
            pid INTEGER, result TEXT, metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT, finished_at TEXT, heartbeat_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_provider_attempt_status ON provider_execution_attempts(status, heartbeat_at);
        ALTER TABLE provider_execution_artifacts ADD COLUMN attempt_id INTEGER REFERENCES provider_execution_attempts(id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_artifact_attempt ON provider_execution_artifacts(attempt_id) WHERE attempt_id IS NOT NULL;
    """),
    (4, """
        CREATE TABLE IF NOT EXISTS github_mutation_plans(
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS github_plans_no_update
        BEFORE UPDATE ON github_mutation_plans BEGIN SELECT RAISE(ABORT, 'github mutation plans are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS github_plans_no_delete
        BEFORE DELETE ON github_mutation_plans BEGIN SELECT RAISE(ABORT, 'github mutation plans are immutable'); END;
        CREATE TABLE IF NOT EXISTS github_mutation_gates(
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL REFERENCES github_mutation_plans(id),
            repo TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','claimed','consumed')),
            decision_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_github_gates_plan ON github_mutation_gates(plan_id,status);
        CREATE TABLE IF NOT EXISTS github_idempotency(
            repo TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(repo,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS github_mutation_reports(
            id INTEGER PRIMARY KEY,
            gate_id INTEGER NOT NULL UNIQUE REFERENCES github_mutation_gates(id),
            plan_id INTEGER NOT NULL REFERENCES github_mutation_plans(id),
            status TEXT NOT NULL CHECK(status IN ('succeeded','partial','failed')),
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """),
    (5, """
        CREATE TABLE IF NOT EXISTS active_workflow_claims(
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            workflow_id TEXT NOT NULL,
            run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,workflow_id)
        );
        INSERT OR IGNORE INTO active_workflow_claims(task_id,workflow_id,run_id)
        SELECT task_id,workflow_id,MIN(id)
          FROM workflow_runs
         WHERE status IN ('running','awaiting_approval')
         GROUP BY task_id,workflow_id;
        CREATE TABLE IF NOT EXISTS pending_provider_gate_claims(
            provider TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            gate_id INTEGER NOT NULL UNIQUE REFERENCES provider_execution_gates(id),
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(provider,agent_id,task_id)
        );
        INSERT OR IGNORE INTO pending_provider_gate_claims(provider,agent_id,task_id,gate_id)
        SELECT provider,agent_id,task_id,MIN(id)
          FROM provider_execution_gates
         WHERE status='pending'
        GROUP BY provider,agent_id,task_id;
    """),
    (6, """
        ALTER TABLE provider_execution_gates
            ADD COLUMN request_hash TEXT
            CHECK(request_hash IS NULL OR (length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'));
        ALTER TABLE provider_execution_gates
            ADD COLUMN definition_hash TEXT
            CHECK(definition_hash IS NULL OR (length(definition_hash)=64 AND definition_hash NOT GLOB '*[^0-9a-f]*'));
        UPDATE provider_execution_gates
           SET status='rejected',
               decision_note=CASE
                   WHEN decision_note='' THEN 'Approval predates immutable snapshot binding; request a new gate.'
                   ELSE decision_note || char(10) || 'Approval predates immutable snapshot binding; request a new gate.'
               END,
               decided_at=COALESCE(decided_at,CURRENT_TIMESTAMP)
         WHERE status IN ('pending','approved');
        DELETE FROM pending_provider_gate_claims
         WHERE gate_id IN (
             SELECT id FROM provider_execution_gates WHERE status='rejected'
         );
        CREATE TRIGGER IF NOT EXISTS provider_gate_snapshot_no_update
        BEFORE UPDATE OF provider,agent_id,task_id,request_hash,definition_hash
        ON provider_execution_gates
        WHEN OLD.provider IS NOT NEW.provider
          OR OLD.agent_id IS NOT NEW.agent_id
          OR OLD.task_id IS NOT NEW.task_id
          OR OLD.request_hash IS NOT NEW.request_hash
          OR OLD.definition_hash IS NOT NEW.definition_hash
        BEGIN
            SELECT RAISE(ABORT, 'provider approval snapshot is immutable');
        END;
    """),
    (7, """
        CREATE TABLE IF NOT EXISTS reviewer_assignments(
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage TEXT NOT NULL,
            reviewer_agent_id TEXT NOT NULL,
            reviewer_provider TEXT NOT NULL,
            reviewer_model TEXT NOT NULL,
            reviewed_stages TEXT NOT NULL,
            reviewed_artifact_ids TEXT NOT NULL,
            producer_agents TEXT NOT NULL,
            excluded_models TEXT NOT NULL,
            excluded_candidates TEXT NOT NULL,
            strategy TEXT NOT NULL,
            review_artifact_id INTEGER REFERENCES artifacts(id),
            verdict TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            UNIQUE(run_id,stage)
        );
        CREATE INDEX IF NOT EXISTS idx_reviewer_assignments_rotation
            ON reviewer_assignments(stage,reviewer_agent_id,id);
    """),
    (8, """
        CREATE TABLE IF NOT EXISTS runtime_settings(
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS runtime_setting_versions(
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            version INTEGER NOT NULL,
            value_json TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(key,version)
        );
        CREATE TRIGGER IF NOT EXISTS runtime_setting_versions_no_update
        BEFORE UPDATE ON runtime_setting_versions
        BEGIN SELECT RAISE(ABORT, 'runtime setting history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS runtime_setting_versions_no_delete
        BEFORE DELETE ON runtime_setting_versions
        BEGIN SELECT RAISE(ABORT, 'runtime setting history is immutable'); END;
    """),
    (9, """
        ALTER TABLE work_items ADD COLUMN identity TEXT;
        ALTER TABLE work_items ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
        ALTER TABLE work_items ADD COLUMN kind TEXT NOT NULL DEFAULT 'task';
        ALTER TABLE work_items ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN inputs_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE work_items ADD COLUMN expected_outputs_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN acceptance_criteria_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN permissions_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN budget_max_tokens INTEGER NOT NULL DEFAULT 4000;
        ALTER TABLE work_items ADD COLUMN budget_max_seconds INTEGER NOT NULL DEFAULT 120;
        ALTER TABLE work_items ADD COLUMN budget_max_cost_usd REAL NOT NULL DEFAULT 0.0;
        ALTER TABLE work_items ADD COLUMN artifact_ids_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN github_number INTEGER;
        ALTER TABLE work_items ADD COLUMN updated_at TEXT;

        UPDATE work_items SET
            identity='work-item:' || id,
            kind=COALESCE(json_extract(payload,'$.kind'),'task'),
            dependencies_json=COALESCE(json_extract(payload,'$.dependencies'),'[]'),
            inputs_json=COALESCE(json_extract(payload,'$.inputs'),'{}'),
            expected_outputs_json=COALESCE(json_extract(payload,'$.expected_outputs'),'[]'),
            acceptance_criteria_json=COALESCE(json_extract(payload,'$.acceptance_criteria'),'[]'),
            permissions_json=COALESCE(json_extract(payload,'$.permissions'),'[]'),
            budget_max_tokens=COALESCE(json_extract(payload,'$.budget.max_tokens'),4000),
            budget_max_seconds=COALESCE(json_extract(payload,'$.budget.max_seconds'),120),
            budget_max_cost_usd=COALESCE(json_extract(payload,'$.budget.max_cost_usd'),0.0),
            artifact_ids_json=COALESCE(json_extract(payload,'$.artifacts'),'[]'),
            github_number=json_extract(payload,'$.github_number'),
            updated_at=created_at
        WHERE json_valid(payload);
        UPDATE work_items SET identity='work-item:' || id, updated_at=created_at
         WHERE identity IS NULL;
        CREATE UNIQUE INDEX idx_work_items_identity ON work_items(identity);

        ALTER TABLE workflow_runs ADD COLUMN identity TEXT;
        ALTER TABLE workflow_runs ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
        UPDATE workflow_runs SET identity='run:' || id;
        CREATE UNIQUE INDEX idx_workflow_runs_identity ON workflow_runs(identity);

        ALTER TABLE artifacts ADD COLUMN identity TEXT;
        ALTER TABLE artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
        UPDATE artifacts SET identity='artifact:' || id;
        CREATE UNIQUE INDEX idx_artifacts_identity ON artifacts(identity);

        ALTER TABLE provider_execution_attempts ADD COLUMN identity TEXT;
        ALTER TABLE provider_execution_attempts ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
        UPDATE provider_execution_attempts SET identity='provider-attempt:' || id;
        CREATE UNIQUE INDEX idx_provider_attempts_identity ON provider_execution_attempts(identity);

        ALTER TABLE provider_execution_artifacts ADD COLUMN identity TEXT;
        ALTER TABLE provider_execution_artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0);
        UPDATE provider_execution_artifacts SET identity='provider-artifact:' || id;
        CREATE UNIQUE INDEX idx_provider_artifacts_identity ON provider_execution_artifacts(identity);

        CREATE TABLE workflow_stages(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','running','waiting_approval','succeeded','failed')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id,stage_key)
        );
        INSERT INTO workflow_stages(identity,run_id,stage_key,status,created_at,updated_at)
        SELECT 'stage:legacy:' || id,id,'legacy',
               CASE status
                   WHEN 'approved' THEN 'succeeded'
                   WHEN 'rejected' THEN 'failed'
                   WHEN 'failed' THEN 'failed'
                   WHEN 'awaiting_approval' THEN 'waiting_approval'
                   ELSE 'running'
               END,
               created_at,COALESCE(completed_at,created_at)
          FROM workflow_runs;

        CREATE TABLE assignments(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER REFERENCES workflow_runs(id),
            stage_id INTEGER REFERENCES workflow_stages(id),
            agent_id TEXT NOT NULL,
            runtime TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','active','suspended','succeeded','failed','cancelled')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO assignments(identity,task_id,agent_id,runtime,status,created_at,updated_at)
        SELECT 'assignment:provider:' || id,task_id,agent_id,provider,
               CASE status
                   WHEN 'claimed' THEN 'active'
                   WHEN 'running' THEN 'active'
                   WHEN 'succeeded' THEN 'succeeded'
                   WHEN 'failed' THEN 'failed'
                   ELSE 'cancelled'
               END,
               created_at,COALESCE(finished_at,heartbeat_at,created_at)
          FROM provider_execution_attempts;

        CREATE TABLE worker_sessions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            runtime TEXT NOT NULL,
            external_session_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('starting','running','suspended','succeeded','failed','cancelled')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO worker_sessions(identity,assignment_id,runtime,status,created_at,updated_at)
        SELECT 'worker-session:provider:' || p.id,a.id,p.provider,
               CASE p.status
                   WHEN 'claimed' THEN 'starting'
                   WHEN 'running' THEN 'running'
                   WHEN 'succeeded' THEN 'succeeded'
                   WHEN 'failed' THEN 'failed'
                   ELSE 'cancelled'
               END,
               p.created_at,COALESCE(p.finished_at,p.heartbeat_at,p.created_at)
          FROM provider_execution_attempts p
          JOIN assignments a ON a.identity='assignment:provider:' || p.id;

        CREATE TABLE attempts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            provider_attempt_id INTEGER UNIQUE REFERENCES provider_execution_attempts(id),
            ordinal INTEGER NOT NULL CHECK(ordinal > 0),
            status TEXT NOT NULL CHECK(status IN ('claimed','running','succeeded','failed','abandoned','cancelled')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assignment_id,ordinal)
        );
        INSERT INTO attempts(identity,assignment_id,provider_attempt_id,ordinal,status,created_at,updated_at)
        SELECT 'attempt:provider:' || p.id,a.id,p.id,1,p.status,p.created_at,
               COALESCE(p.finished_at,p.heartbeat_at,p.created_at)
          FROM provider_execution_attempts p
          JOIN assignments a ON a.identity='assignment:provider:' || p.id;

        CREATE TABLE leases(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
            status TEXT NOT NULL CHECK(status IN ('active','expired','released','revoked')),
            expires_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assignment_id,fencing_token)
        );

        CREATE TABLE worktrees(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            attempt_id INTEGER REFERENCES attempts(id),
            repository TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            branch TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('provisioning','ready','dirty','retained','cleaned','missing')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TRIGGER work_items_identity_required
        BEFORE INSERT ON work_items WHEN NEW.identity IS NULL
        BEGIN SELECT RAISE(ABORT, 'work item identity is required'); END;
        CREATE TRIGGER workflow_runs_identity_required
        BEFORE INSERT ON workflow_runs WHEN NEW.identity IS NULL
        BEGIN SELECT RAISE(ABORT, 'workflow run identity is required'); END;
        CREATE TRIGGER artifacts_identity_required
        BEFORE INSERT ON artifacts WHEN NEW.identity IS NULL
        BEGIN SELECT RAISE(ABORT, 'artifact identity is required'); END;
        CREATE TRIGGER provider_attempts_identity_required
        BEFORE INSERT ON provider_execution_attempts WHEN NEW.identity IS NULL
        BEGIN SELECT RAISE(ABORT, 'provider attempt identity is required'); END;
        CREATE TRIGGER provider_artifacts_identity_required
        BEFORE INSERT ON provider_execution_artifacts WHEN NEW.identity IS NULL
        BEGIN SELECT RAISE(ABORT, 'provider artifact identity is required'); END;

        CREATE TRIGGER work_items_identity_immutable BEFORE UPDATE OF identity ON work_items
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'work item identity is immutable'); END;
        CREATE TRIGGER workflow_runs_identity_immutable BEFORE UPDATE OF identity ON workflow_runs
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'workflow run identity is immutable'); END;
        CREATE TRIGGER artifacts_identity_immutable BEFORE UPDATE OF identity ON artifacts
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'artifact identity is immutable'); END;
        CREATE TRIGGER provider_attempts_identity_immutable BEFORE UPDATE OF identity ON provider_execution_attempts
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'provider attempt identity is immutable'); END;
        CREATE TRIGGER provider_artifacts_identity_immutable BEFORE UPDATE OF identity ON provider_execution_artifacts
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'provider artifact identity is immutable'); END;
        CREATE TRIGGER workflow_stages_identity_immutable BEFORE UPDATE OF identity ON workflow_stages
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'stage identity is immutable'); END;
        CREATE TRIGGER assignments_identity_immutable BEFORE UPDATE OF identity ON assignments
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'assignment identity is immutable'); END;
        CREATE TRIGGER worker_sessions_identity_immutable BEFORE UPDATE OF identity ON worker_sessions
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'worker session identity is immutable'); END;
        CREATE TRIGGER attempts_identity_immutable BEFORE UPDATE OF identity ON attempts
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'attempt identity is immutable'); END;
        CREATE TRIGGER leases_identity_immutable BEFORE UPDATE OF identity ON leases
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'lease identity is immutable'); END;
        CREATE TRIGGER worktrees_identity_immutable BEFORE UPDATE OF identity ON worktrees
        WHEN OLD.identity IS NOT NEW.identity BEGIN SELECT RAISE(ABORT, 'worktree identity is immutable'); END;

        CREATE TRIGGER workflow_runs_valid_transition BEFORE UPDATE OF status ON workflow_runs
        WHEN NOT (
            (OLD.status='running' AND NEW.status IN ('awaiting_approval','failed')) OR
            (OLD.status='awaiting_approval' AND NEW.status IN ('approved','rejected','failed'))
        ) BEGIN SELECT RAISE(ABORT, 'invalid workflow run transition'); END;
        CREATE TRIGGER work_items_valid_transition BEFORE UPDATE OF status ON work_items
        WHEN NOT (
            (OLD.status='pending' AND NEW.status IN ('running','failed')) OR
            (OLD.status='running' AND NEW.status IN ('completed','failed')) OR
            (OLD.status='completed' AND NEW.status IN ('approved','rejected')) OR
            (OLD.status='failed' AND NEW.status='pending')
        ) BEGIN SELECT RAISE(ABORT, 'invalid work item transition'); END;
        CREATE TRIGGER artifacts_valid_transition BEFORE UPDATE OF status ON artifacts
        WHEN NOT (OLD.status='pending' AND NEW.status IN ('approved','rejected'))
        BEGIN SELECT RAISE(ABORT, 'invalid artifact transition'); END;
        CREATE TRIGGER provider_attempts_valid_transition BEFORE UPDATE OF status ON provider_execution_attempts
        WHEN NOT (OLD.status IN ('claimed','running') AND NEW.status IN ('running','succeeded','failed','abandoned') AND OLD.status<>NEW.status)
        BEGIN SELECT RAISE(ABORT, 'invalid provider attempt transition'); END;
        CREATE TRIGGER workflow_stages_valid_transition BEFORE UPDATE OF status ON workflow_stages
        WHEN NOT (
            (OLD.status='pending' AND NEW.status IN ('running','failed')) OR
            (OLD.status='running' AND NEW.status IN ('waiting_approval','succeeded','failed')) OR
            (OLD.status='waiting_approval' AND NEW.status IN ('running','succeeded','failed')) OR
            (OLD.status='failed' AND NEW.status='pending')
        ) BEGIN SELECT RAISE(ABORT, 'invalid stage transition'); END;
        CREATE TRIGGER assignments_valid_transition BEFORE UPDATE OF status ON assignments
        WHEN NOT (
            (OLD.status='pending' AND NEW.status IN ('active','cancelled')) OR
            (OLD.status='active' AND NEW.status IN ('suspended','succeeded','failed','cancelled')) OR
            (OLD.status='suspended' AND NEW.status IN ('active','failed','cancelled'))
        ) BEGIN SELECT RAISE(ABORT, 'invalid assignment transition'); END;
        CREATE TRIGGER worker_sessions_valid_transition BEFORE UPDATE OF status ON worker_sessions
        WHEN NOT (
            (OLD.status='starting' AND NEW.status IN ('running','failed','cancelled')) OR
            (OLD.status='running' AND NEW.status IN ('suspended','succeeded','failed','cancelled')) OR
            (OLD.status='suspended' AND NEW.status IN ('running','failed','cancelled'))
        ) BEGIN SELECT RAISE(ABORT, 'invalid worker session transition'); END;
        CREATE TRIGGER attempts_valid_transition BEFORE UPDATE OF status ON attempts
        WHEN NOT (OLD.status IN ('claimed','running') AND NEW.status IN ('running','succeeded','failed','abandoned','cancelled') AND OLD.status<>NEW.status)
        BEGIN SELECT RAISE(ABORT, 'invalid attempt transition'); END;
        CREATE TRIGGER leases_valid_transition BEFORE UPDATE OF status ON leases
        WHEN NOT (OLD.status='active' AND NEW.status IN ('expired','released','revoked'))
        BEGIN SELECT RAISE(ABORT, 'invalid lease transition'); END;
        CREATE TRIGGER worktrees_valid_transition BEFORE UPDATE OF status ON worktrees
        WHEN NOT (
            (OLD.status='provisioning' AND NEW.status IN ('ready','missing','cleaned')) OR
            (OLD.status='ready' AND NEW.status IN ('dirty','retained','missing')) OR
            (OLD.status='dirty' AND NEW.status IN ('ready','retained','missing')) OR
            (OLD.status='retained' AND NEW.status IN ('cleaned','missing')) OR
            (OLD.status='missing' AND NEW.status IN ('provisioning','cleaned'))
        ) BEGIN SELECT RAISE(ABORT, 'invalid worktree transition'); END;
    """),
    (10, """
        ALTER TABLE events ADD COLUMN identity TEXT;
        ALTER TABLE events ADD COLUMN correlation_root TEXT;
        ALTER TABLE events ADD COLUMN correlation_json TEXT;
        ALTER TABLE events ADD COLUMN previous_hash TEXT;
        ALTER TABLE events ADD COLUMN record_hash TEXT;

        CREATE TABLE outbox_messages(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
            topic TEXT NOT NULL,
            payload TEXT NOT NULL,
            correlation_json TEXT NOT NULL,
            delivery_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','dispatching','delivered','failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            claimed_by TEXT,
            claim_token TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            delivered_at TEXT
        );
        CREATE INDEX idx_outbox_ready
            ON outbox_messages(status,available_at,id);
        CREATE TRIGGER outbox_valid_transition
        BEFORE UPDATE OF status ON outbox_messages
        WHEN NOT (
            (OLD.status IN ('pending','failed') AND NEW.status='dispatching') OR
            (OLD.status='dispatching' AND NEW.status IN ('delivered','failed'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid outbox transition'); END;
    """),
    (11, """
        ALTER TABLE artifacts ADD COLUMN digest TEXT;
        ALTER TABLE artifacts ADD COLUMN producer_json TEXT;
        ALTER TABLE artifacts ADD COLUMN verifier_json TEXT;
        ALTER TABLE artifacts ADD COLUMN inputs_json TEXT;
        ALTER TABLE artifacts ADD COLUMN toolchain_json TEXT;
        ALTER TABLE artifacts ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'review';

        ALTER TABLE provider_execution_artifacts ADD COLUMN digest TEXT;
        ALTER TABLE provider_execution_artifacts ADD COLUMN producer_json TEXT;
        ALTER TABLE provider_execution_artifacts ADD COLUMN verifier_json TEXT;
        ALTER TABLE provider_execution_artifacts ADD COLUMN inputs_json TEXT;
        ALTER TABLE provider_execution_artifacts ADD COLUMN toolchain_json TEXT;

        CREATE TABLE criterion_evidence(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            criterion_index INTEGER NOT NULL CHECK(criterion_index >= 0),
            criterion_text TEXT NOT NULL,
            artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
            evidence_type TEXT NOT NULL
                CHECK(evidence_type IN ('test_result','diff','review','summary')),
            primary_evidence INTEGER NOT NULL CHECK(primary_evidence IN (0,1)),
            artifact_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed'
                CHECK(status IN ('proposed','accepted','rejected')),
            verifier_json TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            UNIQUE(task_id,criterion_index,artifact_id,evidence_type)
        );
        CREATE INDEX idx_criterion_evidence_status
            ON criterion_evidence(task_id,criterion_index,status,primary_evidence);
        CREATE TRIGGER artifacts_evidence_envelope_required
        BEFORE INSERT ON artifacts
        WHEN NEW.digest IS NULL OR NEW.producer_json IS NULL
          OR NEW.verifier_json IS NULL OR NEW.inputs_json IS NULL
          OR NEW.toolchain_json IS NULL
        BEGIN SELECT RAISE(ABORT, 'artifact evidence envelope is required'); END;
        CREATE TRIGGER provider_artifacts_evidence_envelope_required
        BEFORE INSERT ON provider_execution_artifacts
        WHEN NEW.digest IS NULL OR NEW.producer_json IS NULL
          OR NEW.verifier_json IS NULL OR NEW.inputs_json IS NULL
          OR NEW.toolchain_json IS NULL
        BEGIN SELECT RAISE(ABORT, 'provider artifact evidence envelope is required'); END;
        CREATE TRIGGER criterion_evidence_identity_immutable
        BEFORE UPDATE OF identity ON criterion_evidence
        WHEN OLD.identity IS NOT NEW.identity
        BEGIN SELECT RAISE(ABORT, 'criterion evidence identity is immutable'); END;
        CREATE TRIGGER criterion_evidence_accepted_no_update
        BEFORE UPDATE ON criterion_evidence WHEN OLD.status='accepted'
        BEGIN SELECT RAISE(ABORT, 'accepted evidence is immutable'); END;
        CREATE TRIGGER criterion_evidence_accepted_no_delete
        BEFORE DELETE ON criterion_evidence WHEN OLD.status='accepted'
        BEGIN SELECT RAISE(ABORT, 'accepted evidence is immutable'); END;
        CREATE TRIGGER artifacts_accepted_evidence_no_update
        BEFORE UPDATE ON artifacts
        WHEN EXISTS(
            SELECT 1 FROM criterion_evidence e
             WHERE e.artifact_id=OLD.id AND e.status='accepted'
        )
        BEGIN SELECT RAISE(ABORT, 'accepted evidence artifact is immutable'); END;
        CREATE TRIGGER artifacts_accepted_evidence_no_delete
        BEFORE DELETE ON artifacts
        WHEN EXISTS(
            SELECT 1 FROM criterion_evidence e
             WHERE e.artifact_id=OLD.id AND e.status='accepted'
        )
        BEGIN SELECT RAISE(ABORT, 'accepted evidence artifact is immutable'); END;
    """),
    (12, """
        CREATE TABLE policy_state(
            id INTEGER PRIMARY KEY CHECK(id=1),
            emergency_stop INTEGER NOT NULL DEFAULT 0 CHECK(emergency_stop IN (0,1)),
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'system',
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO policy_state(id) VALUES(1);

        CREATE TABLE policy_decisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            request_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('allow','deny','require_approval')),
            reason TEXT NOT NULL,
            policy_version INTEGER NOT NULL CHECK(policy_version > 0),
            approval_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER policy_decisions_no_update BEFORE UPDATE ON policy_decisions
        BEGIN SELECT RAISE(ABORT, 'policy decisions are immutable'); END;
        CREATE TRIGGER policy_decisions_no_delete BEFORE DELETE ON policy_decisions
        BEGIN SELECT RAISE(ABORT, 'policy decisions are immutable'); END;

        CREATE TABLE scoped_execution_approvals(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER REFERENCES workflow_runs(id),
            stage_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            runtime_id TEXT NOT NULL,
            worktree_id TEXT,
            permissions_json TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','consumed','expired','cancelled')),
            requested_by TEXT NOT NULL,
            decided_by TEXT,
            decision_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            consumed_at TEXT
        );
        CREATE INDEX idx_scoped_approvals_status
            ON scoped_execution_approvals(status,expires_at,task_id);
        CREATE TRIGGER scoped_approval_scope_immutable
        BEFORE UPDATE OF mission_id,task_id,run_id,stage_id,worker_id,runtime_id,
                         worktree_id,permissions_json,request_digest
        ON scoped_execution_approvals
        BEGIN SELECT RAISE(ABORT, 'approval scope is immutable'); END;
        CREATE TRIGGER scoped_approval_valid_transition
        BEFORE UPDATE OF status ON scoped_execution_approvals
        WHEN NOT (
            (OLD.status='pending' AND NEW.status IN ('approved','rejected','expired','cancelled')) OR
            (OLD.status='approved' AND NEW.status IN ('consumed','expired','cancelled'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid scoped approval transition'); END;
    """),
    (13, """
        CREATE TABLE worker_qualifications(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            role TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('qualified','failed','expired','quarantined')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            valid_until TEXT NOT NULL
        );
        CREATE INDEX idx_worker_qualification_route
            ON worker_qualifications(status,role,valid_until,worker_id,id);
        CREATE TRIGGER worker_qualifications_no_update BEFORE UPDATE ON worker_qualifications
        BEGIN SELECT RAISE(ABORT, 'worker qualifications are immutable'); END;
        CREATE TRIGGER worker_qualifications_no_delete BEFORE DELETE ON worker_qualifications
        BEGIN SELECT RAISE(ABORT, 'worker qualifications are immutable'); END;

        CREATE TABLE worker_lifecycle(
            worker_id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('active','draining','quarantined','offline')),
            reason TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE worker_handoffs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            source_worker_id TEXT NOT NULL,
            replacement_worker_id TEXT NOT NULL,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER REFERENCES workflow_runs(id),
            stage_id TEXT NOT NULL,
            attempt_id TEXT,
            context_digest TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER worker_handoffs_no_update BEFORE UPDATE ON worker_handoffs
        BEGIN SELECT RAISE(ABORT, 'worker handoffs are immutable'); END;
        CREATE TRIGGER worker_handoffs_no_delete BEFORE DELETE ON worker_handoffs
        BEGIN SELECT RAISE(ABORT, 'worker handoffs are immutable'); END;
    """),
    (14, """
        ALTER TABLE workflow_runs ADD COLUMN workflow_version TEXT NOT NULL DEFAULT 'legacy';
        ALTER TABLE workflow_runs ADD COLUMN definition_digest TEXT;
        ALTER TABLE workflow_runs ADD COLUMN definition_json TEXT;
        ALTER TABLE workflow_runs ADD COLUMN checkpoint_sequence INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE workflow_stages ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE workflow_stages ADD COLUMN definition_json TEXT;
        CREATE TRIGGER workflow_run_definition_immutable
        BEFORE UPDATE OF workflow_id,workflow_version,definition_digest,definition_json
        ON workflow_runs
        BEGIN SELECT RAISE(ABORT, 'workflow definition is immutable for a run'); END;

        CREATE TABLE stage_checkpoints(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            state TEXT NOT NULL CHECK(state IN ('pending','running','waiting_approval','succeeded','failed')),
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id,sequence)
        );
        CREATE TRIGGER stage_checkpoints_no_update BEFORE UPDATE ON stage_checkpoints
        BEGIN SELECT RAISE(ABORT, 'stage checkpoints are immutable'); END;
        CREATE TRIGGER stage_checkpoints_no_delete BEFORE DELETE ON stage_checkpoints
        BEGIN SELECT RAISE(ABORT, 'stage checkpoints are immutable'); END;

        CREATE TABLE workflow_mutations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            operation TEXT NOT NULL CHECK(operation IN ('provider_call','worktree','github')),
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved'
                CHECK(status IN ('reserved','completed','failed')),
            result_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            UNIQUE(run_id,operation,idempotency_key)
        );
        CREATE TRIGGER workflow_mutation_scope_immutable
        BEFORE UPDATE OF run_id,stage_id,operation,idempotency_key,request_digest
        ON workflow_mutations
        BEGIN SELECT RAISE(ABORT, 'workflow mutation scope is immutable'); END;
    """),
    (15, """
        CREATE TRIGGER leases_no_delete
        BEFORE DELETE ON leases
        BEGIN SELECT RAISE(ABORT, 'lease history is immutable'); END;

        CREATE TABLE assignment_conflict_domains(
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            domain TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(assignment_id,domain)
        );
        CREATE INDEX idx_assignment_conflict_domains_domain
            ON assignment_conflict_domains(domain,assignment_id);
        CREATE TRIGGER assignment_conflict_domains_no_update
        BEFORE UPDATE ON assignment_conflict_domains
        BEGIN SELECT RAISE(ABORT, 'assignment conflict domains are immutable'); END;
        CREATE TRIGGER assignment_conflict_domains_no_delete
        BEFORE DELETE ON assignment_conflict_domains
        BEGIN SELECT RAISE(ABORT, 'assignment conflict domains are immutable'); END;

        CREATE TABLE scheduler_conflicts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            requested_domains_json TEXT NOT NULL,
            conflicting_assignment_ids_json TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('serialize','escalate')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER scheduler_conflicts_no_update
        BEFORE UPDATE ON scheduler_conflicts
        BEGIN SELECT RAISE(ABORT, 'scheduler conflicts are immutable'); END;
        CREATE TRIGGER scheduler_conflicts_no_delete
        BEFORE DELETE ON scheduler_conflicts
        BEGIN SELECT RAISE(ABORT, 'scheduler conflicts are immutable'); END;
    """),
    (16, """
        ALTER TABLE worker_sessions ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE worker_sessions ADD COLUMN result_json TEXT;
        ALTER TABLE worker_sessions ADD COLUMN heartbeat_at TEXT;
        ALTER TABLE worker_sessions ADD COLUMN finalized_at TEXT;
        ALTER TABLE worker_sessions ADD COLUMN mutable_action_count INTEGER NOT NULL DEFAULT 0
            CHECK(mutable_action_count >= 0);

        CREATE TABLE runtime_session_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            session_id INTEGER NOT NULL REFERENCES worker_sessions(id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            kind TEXT NOT NULL CHECK(kind IN (
                'status','message','tool_call','artifact','heartbeat','error'
            )),
            payload_json TEXT NOT NULL,
            mutable INTEGER NOT NULL DEFAULT 0 CHECK(mutable IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id,sequence)
        );
        CREATE INDEX idx_runtime_session_events
            ON runtime_session_events(session_id,sequence);
        CREATE TRIGGER runtime_session_events_no_update
        BEFORE UPDATE ON runtime_session_events
        BEGIN SELECT RAISE(ABORT, 'runtime session events are immutable'); END;
        CREATE TRIGGER runtime_session_events_no_delete
        BEFORE DELETE ON runtime_session_events
        BEGIN SELECT RAISE(ABORT, 'runtime session events are immutable'); END;
        CREATE TRIGGER worker_session_scope_immutable
        BEFORE UPDATE OF assignment_id,runtime,request_json ON worker_sessions
        BEGIN SELECT RAISE(ABORT, 'worker session scope is immutable'); END;
        CREATE TRIGGER worker_session_external_id_immutable
        BEFORE UPDATE OF external_session_id ON worker_sessions
        WHEN OLD.external_session_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'worker external session identity is immutable'); END;
        CREATE TRIGGER worker_session_result_immutable
        BEFORE UPDATE OF result_json ON worker_sessions
        WHEN OLD.result_json IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'worker session result is immutable'); END;
    """),
    (17, """
        ALTER TABLE worktrees ADD COLUMN task_id INTEGER REFERENCES work_items(id);
        ALTER TABLE worktrees ADD COLUMN lease_id INTEGER REFERENCES leases(id);
        ALTER TABLE worktrees ADD COLUMN fencing_token INTEGER;
        ALTER TABLE worktrees ADD COLUMN owner TEXT;
        ALTER TABLE worktrees ADD COLUMN retention_until TEXT;
        ALTER TABLE worktrees ADD COLUMN reconciled_at TEXT;

        UPDATE worktrees
           SET task_id=(SELECT task_id FROM assignments WHERE id=worktrees.assignment_id),
               owner=(SELECT agent_id FROM assignments WHERE id=worktrees.assignment_id),
               lease_id=(SELECT id FROM leases
                          WHERE assignment_id=worktrees.assignment_id
                          ORDER BY fencing_token DESC LIMIT 1),
               fencing_token=(SELECT fencing_token FROM leases
                               WHERE assignment_id=worktrees.assignment_id
                               ORDER BY fencing_token DESC LIMIT 1);

        CREATE UNIQUE INDEX idx_worktrees_active_assignment ON worktrees(assignment_id)
            WHERE status<>'cleaned';
        CREATE UNIQUE INDEX idx_worktrees_repository_branch
            ON worktrees(repository,branch);
        CREATE INDEX idx_worktrees_reconcile
            ON worktrees(repository,status,retention_until);
        CREATE TRIGGER worktree_authority_required
        BEFORE INSERT ON worktrees
        WHEN NEW.task_id IS NULL OR NEW.lease_id IS NULL
          OR NEW.fencing_token IS NULL OR NEW.owner IS NULL
        BEGIN SELECT RAISE(ABORT, 'worktree authority metadata is required'); END;
        CREATE TRIGGER worktree_scope_immutable
        BEFORE UPDATE OF assignment_id,attempt_id,repository,base_sha,branch,path,
                         task_id,lease_id,fencing_token,owner
        ON worktrees
        BEGIN SELECT RAISE(ABORT, 'worktree authority metadata is immutable'); END;
        CREATE TRIGGER worktrees_no_delete
        BEFORE DELETE ON worktrees
        BEGIN SELECT RAISE(ABORT, 'worktree history is immutable'); END;
    """),
    (18, """
        CREATE TABLE execution_context_packages(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
            digest TEXT NOT NULL UNIQUE
                CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
            package_json TEXT NOT NULL,
            byte_count INTEGER NOT NULL CHECK(byte_count > 0),
            token_count INTEGER NOT NULL CHECK(token_count > 0),
            compacted INTEGER NOT NULL CHECK(compacted IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_context_packages_scope
            ON execution_context_packages(task_id,run_id,assignment_id,id);
        CREATE TRIGGER execution_context_packages_no_update
        BEFORE UPDATE ON execution_context_packages
        BEGIN SELECT RAISE(ABORT, 'execution context packages are immutable'); END;
        CREATE TRIGGER execution_context_packages_no_delete
        BEFORE DELETE ON execution_context_packages
        BEGIN SELECT RAISE(ABORT, 'execution context packages are immutable'); END;

        ALTER TABLE worker_sessions ADD COLUMN context_package_id INTEGER
            REFERENCES execution_context_packages(id);
        ALTER TABLE worker_sessions ADD COLUMN context_digest TEXT;
        CREATE INDEX idx_worker_sessions_context
            ON worker_sessions(context_package_id);
        CREATE TRIGGER worker_session_context_immutable
        BEFORE UPDATE OF context_package_id,context_digest ON worker_sessions
        BEGIN SELECT RAISE(ABORT, 'worker session context is immutable'); END;
    """),
    (19, """
        CREATE TABLE hermes_acp_sessions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            worker_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            attempt_id INTEGER NOT NULL REFERENCES attempts(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
            agent_role TEXT NOT NULL,
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            context_package_id INTEGER NOT NULL REFERENCES execution_context_packages(id),
            allowed_tools_json TEXT NOT NULL,
            executable TEXT NOT NULL,
            hermes_version TEXT NOT NULL,
            protocol_version INTEGER NOT NULL CHECK(protocol_version > 0),
            external_session_id TEXT NOT NULL UNIQUE,
            process_pid INTEGER,
            status TEXT NOT NULL CHECK(status IN (
                'running','suspended','succeeded','failed','cancelled'
            )),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_hermes_acp_assignment
            ON hermes_acp_sessions(assignment_id,status,id);
        CREATE TRIGGER hermes_acp_scope_immutable
        BEFORE UPDATE OF worker_session_id,task_id,run_id,stage_id,attempt_id,
                         assignment_id,fencing_token,agent_role,worktree_id,
                         context_package_id,allowed_tools_json,executable,
                         hermes_version,protocol_version,external_session_id
        ON hermes_acp_sessions
        BEGIN SELECT RAISE(ABORT, 'Hermes ACP session scope is immutable'); END;
        CREATE TRIGGER hermes_acp_sessions_no_delete
        BEFORE DELETE ON hermes_acp_sessions
        BEGIN SELECT RAISE(ABORT, 'Hermes ACP session history is immutable'); END;
    """),
    (20, """
        CREATE TABLE stage_approval_consumptions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            approval_id INTEGER NOT NULL UNIQUE REFERENCES scoped_execution_approvals(id),
            attempt_id INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            request_digest TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_stage_approval_scope
            ON stage_approval_consumptions(run_id,stage_id,assignment_id);
        CREATE TRIGGER stage_approval_consumptions_no_update
        BEFORE UPDATE ON stage_approval_consumptions
        BEGIN SELECT RAISE(ABORT, 'stage approval consumption is immutable'); END;
        CREATE TRIGGER stage_approval_consumptions_no_delete
        BEFORE DELETE ON stage_approval_consumptions
        BEGIN SELECT RAISE(ABORT, 'stage approval consumption is immutable'); END;
    """),
    (21, """
        CREATE TABLE codex_worker_results(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            worker_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            approval_consumption_id INTEGER NOT NULL UNIQUE REFERENCES stage_approval_consumptions(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            attempt_id INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            context_package_id INTEGER NOT NULL REFERENCES execution_context_packages(id),
            codex_version TEXT NOT NULL,
            permission_profile_json TEXT NOT NULL,
            invocation_json TEXT NOT NULL,
            executed_commands_json TEXT NOT NULL,
            changed_files_json TEXT NOT NULL,
            diff_digest TEXT NOT NULL CHECK(length(diff_digest)=64),
            status TEXT NOT NULL CHECK(status IN ('succeeded','failed','timed_out','cancelled','output_limited')),
            exit_code INTEGER,
            handoff_json TEXT NOT NULL,
            evidence_directory TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_codex_worker_scope
            ON codex_worker_results(task_id,run_id,stage_id,assignment_id);
        CREATE TRIGGER codex_worker_results_no_update BEFORE UPDATE ON codex_worker_results
        BEGIN SELECT RAISE(ABORT, 'Codex worker result is immutable'); END;
        CREATE TRIGGER codex_worker_results_no_delete BEFORE DELETE ON codex_worker_results
        BEGIN SELECT RAISE(ABORT, 'Codex worker result is immutable'); END;
    """),
    (22, """
        CREATE TABLE validator_results(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            attempt_id INTEGER NOT NULL REFERENCES attempts(id),
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            candidate_digest TEXT NOT NULL CHECK(length(candidate_digest)=64),
            pack_id TEXT NOT NULL,
            pack_digest TEXT NOT NULL CHECK(length(pack_digest)=64),
            category TEXT NOT NULL CHECK(category IN ('test','lint','type_check','build','security_scan')),
            command_json TEXT NOT NULL,
            command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
            status TEXT NOT NULL CHECK(status IN ('succeeded','failed','timed_out','output_limited')),
            exit_code INTEGER,
            stdout TEXT NOT NULL,
            stderr TEXT NOT NULL,
            environment_json TEXT NOT NULL,
            criterion_mappings_json TEXT NOT NULL,
            evidence_directory TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(attempt_id,candidate_digest,category)
        );
        CREATE INDEX idx_validator_candidate
            ON validator_results(task_id,candidate_digest,category);
        CREATE TRIGGER validator_results_no_update BEFORE UPDATE ON validator_results
        BEGIN SELECT RAISE(ABORT, 'validator result is immutable'); END;
        CREATE TRIGGER validator_results_no_delete BEFORE DELETE ON validator_results
        BEGIN SELECT RAISE(ABORT, 'validator result is immutable'); END;
    """),
    (23, """
        CREATE TABLE candidate_change_artifacts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            codex_result_id INTEGER NOT NULL UNIQUE REFERENCES codex_worker_results(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            stable_task_id TEXT NOT NULL,
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            base_sha TEXT NOT NULL,
            head_sha TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL,
            diff_digest TEXT NOT NULL CHECK(length(diff_digest)=64),
            changed_files_json TEXT NOT NULL,
            commit_message TEXT NOT NULL,
            validation_digest TEXT NOT NULL CHECK(length(validation_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER candidate_change_artifacts_no_update BEFORE UPDATE ON candidate_change_artifacts
        BEGIN SELECT RAISE(ABORT, 'candidate change artifact is immutable'); END;
        CREATE TRIGGER candidate_change_artifacts_no_delete BEFORE DELETE ON candidate_change_artifacts
        BEGIN SELECT RAISE(ABORT, 'candidate change artifact is immutable'); END;

        CREATE TABLE candidate_pr_plans(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidate_change_artifacts(id),
            github_plan_id INTEGER NOT NULL UNIQUE REFERENCES github_mutation_plans(id),
            github_gate_id INTEGER NOT NULL UNIQUE REFERENCES github_mutation_gates(id),
            dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run=1),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER candidate_pr_plans_no_update BEFORE UPDATE ON candidate_pr_plans
        BEGIN SELECT RAISE(ABORT, 'candidate PR plan is immutable'); END;
        CREATE TRIGGER candidate_pr_plans_no_delete BEFORE DELETE ON candidate_pr_plans
        BEGIN SELECT RAISE(ABORT, 'candidate PR plan is immutable'); END;
    """),
    (24, """
        ALTER TABLE codex_worker_results
            ADD COLUMN producer_model TEXT NOT NULL DEFAULT 'provider:codex';

        CREATE TABLE evaluation_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            candidate_id INTEGER NOT NULL REFERENCES candidate_change_artifacts(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            candidate_digest TEXT NOT NULL CHECK(length(candidate_digest)=64),
            producer_model TEXT NOT NULL,
            reviewer_agent_id TEXT NOT NULL,
            reviewer_provider TEXT NOT NULL,
            reviewer_model TEXT NOT NULL,
            rubric_id TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            deterministic_evidence_digest TEXT NOT NULL CHECK(length(deterministic_evidence_digest)=64),
            verdict TEXT NOT NULL CHECK(verdict IN ('accepted','rejected')),
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(candidate_id,rubric_id,rubric_version)
        );
        CREATE TABLE criterion_verdicts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            evaluation_id INTEGER NOT NULL REFERENCES evaluation_runs(id),
            criterion_index INTEGER NOT NULL,
            criterion_text TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
            concerns_json TEXT NOT NULL,
            dissent_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(evaluation_id,criterion_index)
        );
        CREATE INDEX idx_evaluation_candidate ON evaluation_runs(candidate_id,rubric_id,rubric_version);
        CREATE TRIGGER evaluation_runs_no_update BEFORE UPDATE ON evaluation_runs
        BEGIN SELECT RAISE(ABORT, 'evaluation run is immutable'); END;
        CREATE TRIGGER evaluation_runs_no_delete BEFORE DELETE ON evaluation_runs
        BEGIN SELECT RAISE(ABORT, 'evaluation run is immutable'); END;
        CREATE TRIGGER criterion_verdicts_no_update BEFORE UPDATE ON criterion_verdicts
        BEGIN SELECT RAISE(ABORT, 'criterion verdict is immutable'); END;
        CREATE TRIGGER criterion_verdicts_no_delete BEFORE DELETE ON criterion_verdicts
        BEGIN SELECT RAISE(ABORT, 'criterion verdict is immutable'); END;
    """),
    (25, """
        CREATE TABLE engineering_loops(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            objective TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            repeated_failure_action TEXT NOT NULL
                CHECK(repeated_failure_action IN ('replan','replace_worker')),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','paused','accepted','failed','escalated')),
            max_iterations INTEGER NOT NULL CHECK(max_iterations > 0),
            max_seconds INTEGER NOT NULL CHECK(max_seconds > 0),
            max_tokens INTEGER NOT NULL CHECK(max_tokens > 0),
            max_cost_usd REAL NOT NULL CHECK(max_cost_usd >= 0),
            max_tool_failures INTEGER NOT NULL CHECK(max_tool_failures >= 0),
            current_iteration INTEGER NOT NULL DEFAULT 0 CHECK(current_iteration >= 0),
            consumed_seconds INTEGER NOT NULL DEFAULT 0 CHECK(consumed_seconds >= 0),
            consumed_tokens INTEGER NOT NULL DEFAULT 0 CHECK(consumed_tokens >= 0),
            consumed_cost_usd REAL NOT NULL DEFAULT 0 CHECK(consumed_cost_usd >= 0),
            tool_failures INTEGER NOT NULL DEFAULT 0 CHECK(tool_failures >= 0),
            last_failure_signature TEXT,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0
                CHECK(consecutive_failure_count >= 0),
            termination_reason TEXT,
            termination_actor TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE engineering_iterations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            loop_id INTEGER NOT NULL REFERENCES engineering_loops(id),
            iteration_number INTEGER NOT NULL CHECK(iteration_number > 0),
            objective TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            diff_digest TEXT NOT NULL CHECK(length(diff_digest)=64),
            validator_results_json TEXT NOT NULL,
            critic_result_json TEXT NOT NULL,
            budget_usage_json TEXT NOT NULL,
            failure_signature TEXT,
            consecutive_failure_count INTEGER NOT NULL CHECK(consecutive_failure_count >= 0),
            accepted_evidence INTEGER NOT NULL CHECK(accepted_evidence IN (0,1)),
            outcome TEXT NOT NULL CHECK(outcome IN (
                'repair','replan','replace_worker','paused','accepted','failed'
            )),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(loop_id,iteration_number)
        );
        CREATE TABLE engineering_loop_limit_revisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            loop_id INTEGER NOT NULL REFERENCES engineering_loops(id),
            previous_limits_json TEXT NOT NULL,
            new_limits_json TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approval_note TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_engineering_iterations_loop
            ON engineering_iterations(loop_id,iteration_number);
        CREATE TRIGGER engineering_loop_scope_immutable
        BEFORE UPDATE OF run_id,task_id,objective,worker_id,repeated_failure_action
        ON engineering_loops
        BEGIN SELECT RAISE(ABORT, 'engineering loop scope is immutable'); END;
        CREATE TRIGGER engineering_loop_transition
        BEFORE UPDATE OF status ON engineering_loops
        WHEN NOT (
            OLD.status=NEW.status OR
            (OLD.status='active' AND NEW.status IN ('paused','accepted','failed','escalated')) OR
            (OLD.status='paused' AND NEW.status IN ('active','failed','escalated'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid engineering loop transition'); END;
        CREATE TRIGGER engineering_loop_accept_requires_evidence
        BEFORE UPDATE OF status ON engineering_loops
        WHEN NEW.status='accepted' AND NOT EXISTS (
            SELECT 1 FROM engineering_iterations i
             WHERE i.loop_id=NEW.id AND i.outcome='accepted' AND i.accepted_evidence=1
        )
        BEGIN SELECT RAISE(ABORT, 'engineering loop acceptance requires evidence'); END;
        CREATE TRIGGER engineering_loop_failure_requires_iteration
        BEFORE UPDATE OF status ON engineering_loops
        WHEN NEW.status='failed' AND NOT EXISTS (
            SELECT 1 FROM engineering_iterations i
             WHERE i.loop_id=NEW.id AND i.outcome='failed'
        )
        BEGIN SELECT RAISE(ABORT, 'engineering loop failure requires an explicit iteration'); END;
        CREATE TRIGGER engineering_loop_escalation_requires_actor
        BEFORE UPDATE OF status ON engineering_loops
        WHEN NEW.status='escalated' AND (
            NEW.termination_actor IS NULL OR trim(NEW.termination_actor)='' OR
            NEW.termination_reason IS NULL OR trim(NEW.termination_reason)=''
        )
        BEGIN SELECT RAISE(ABORT, 'engineering loop escalation requires a human actor'); END;
        CREATE TRIGGER engineering_loop_terminal_no_update
        BEFORE UPDATE ON engineering_loops
        WHEN OLD.status IN ('accepted','failed','escalated')
        BEGIN SELECT RAISE(ABORT, 'terminal engineering loop is immutable'); END;
        CREATE TRIGGER engineering_loops_no_delete BEFORE DELETE ON engineering_loops
        BEGIN SELECT RAISE(ABORT, 'engineering loop history is immutable'); END;
        CREATE TRIGGER engineering_iterations_no_update BEFORE UPDATE ON engineering_iterations
        BEGIN SELECT RAISE(ABORT, 'engineering iteration is immutable'); END;
        CREATE TRIGGER engineering_iterations_no_delete BEFORE DELETE ON engineering_iterations
        BEGIN SELECT RAISE(ABORT, 'engineering iteration is immutable'); END;
        CREATE TRIGGER engineering_limit_revisions_no_update BEFORE UPDATE ON engineering_loop_limit_revisions
        BEGIN SELECT RAISE(ABORT, 'engineering limit revision is immutable'); END;
        CREATE TRIGGER engineering_limit_revisions_no_delete BEFORE DELETE ON engineering_loop_limit_revisions
        BEGIN SELECT RAISE(ABORT, 'engineering limit revision is immutable'); END;
    """),
    (26, """
        CREATE TABLE coding_delivery_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            logical_attempt_key TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stable_task_id TEXT NOT NULL,
            engineering_loop_id INTEGER NOT NULL UNIQUE REFERENCES engineering_loops(id),
            initial_worker_id TEXT NOT NULL,
            current_worker_id TEXT NOT NULL,
            max_repair_iterations INTEGER NOT NULL CHECK(max_repair_iterations > 0),
            repair_iterations INTEGER NOT NULL DEFAULT 0 CHECK(repair_iterations >= 0),
            last_failure_signature TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN (
                'active','awaiting_founder','pr_ready','rejected','failed'
            )),
            candidate_id INTEGER REFERENCES candidate_change_artifacts(id),
            evaluation_id INTEGER REFERENCES evaluation_runs(id),
            founder_gate_id INTEGER REFERENCES approval_gates(id),
            github_plan_id INTEGER REFERENCES github_mutation_plans(id),
            github_gate_id INTEGER REFERENCES github_mutation_gates(id),
            terminal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE coding_delivery_iterations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            delivery_id INTEGER NOT NULL REFERENCES coding_delivery_runs(id),
            iteration_number INTEGER NOT NULL CHECK(iteration_number > 0),
            codex_result_id INTEGER NOT NULL UNIQUE REFERENCES codex_worker_results(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            worker_id TEXT NOT NULL,
            validator_snapshot_json TEXT NOT NULL,
            candidate_id INTEGER REFERENCES candidate_change_artifacts(id),
            evaluation_id INTEGER REFERENCES evaluation_runs(id),
            outcome TEXT NOT NULL CHECK(outcome IN (
                'validation_failed','review_rejected','awaiting_founder','repair_exhausted'
            )),
            selected_repair_worker_id TEXT,
            failure_signature TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(delivery_id,iteration_number)
        );
        CREATE INDEX idx_coding_delivery_task ON coding_delivery_runs(task_id,status,id);
        CREATE TRIGGER coding_delivery_scope_immutable
        BEFORE UPDATE OF logical_attempt_key,task_id,run_id,stable_task_id,
                         engineering_loop_id,initial_worker_id,max_repair_iterations
        ON coding_delivery_runs
        BEGIN SELECT RAISE(ABORT, 'coding delivery scope is immutable'); END;
        CREATE TRIGGER coding_delivery_terminal_no_update
        BEFORE UPDATE ON coding_delivery_runs
        WHEN OLD.status IN ('pr_ready','rejected','failed')
        BEGIN SELECT RAISE(ABORT, 'terminal coding delivery is immutable'); END;
        CREATE TRIGGER coding_delivery_runs_no_delete BEFORE DELETE ON coding_delivery_runs
        BEGIN SELECT RAISE(ABORT, 'coding delivery history is immutable'); END;
        CREATE TRIGGER coding_delivery_iterations_no_update BEFORE UPDATE ON coding_delivery_iterations
        BEGIN SELECT RAISE(ABORT, 'coding delivery iteration is immutable'); END;
        CREATE TRIGGER coding_delivery_iterations_no_delete BEFORE DELETE ON coding_delivery_iterations
        BEGIN SELECT RAISE(ABORT, 'coding delivery iteration is immutable'); END;
    """),
    (27, """
        CREATE TABLE execution_traces(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            correlation_root TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','paused','completed','failed')),
            max_tokens INTEGER NOT NULL CHECK(max_tokens > 0),
            max_cost_usd REAL NOT NULL CHECK(max_cost_usd >= 0),
            max_stages INTEGER NOT NULL CHECK(max_stages > 0),
            max_retries INTEGER NOT NULL CHECK(max_retries >= 0),
            max_tool_calls INTEGER NOT NULL CHECK(max_tool_calls >= 0),
            duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0),
            retries INTEGER NOT NULL DEFAULT 0 CHECK(retries >= 0),
            tokens INTEGER NOT NULL DEFAULT 0 CHECK(tokens >= 0),
            estimated_cost_usd REAL NOT NULL DEFAULT 0 CHECK(estimated_cost_usd >= 0),
            tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(tool_calls >= 0),
            stages_reserved INTEGER NOT NULL DEFAULT 0 CHECK(stages_reserved >= 0),
            terminal_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE execution_trace_links(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            entity_type TEXT NOT NULL CHECK(entity_type IN (
                'task','workflow','hermes_session','worker_process','worktree',
                'validator','stage_approval','founder_approval','github_approval',
                'coding_delivery','candidate','evaluation'
            )),
            entity_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id,entity_type,entity_id)
        );
        CREATE TABLE execution_usage_samples(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            idempotency_key TEXT NOT NULL,
            stage_key TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            tokens INTEGER NOT NULL CHECK(tokens >= 0),
            estimated_cost_usd REAL NOT NULL CHECK(estimated_cost_usd >= 0),
            tool_calls INTEGER NOT NULL CHECK(tool_calls >= 0),
            terminal_reason TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id,idempotency_key)
        );
        CREATE TABLE execution_stage_reservations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            stage_key TEXT NOT NULL,
            estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens >= 0),
            estimated_cost_usd REAL NOT NULL CHECK(estimated_cost_usd >= 0),
            estimated_tool_calls INTEGER NOT NULL CHECK(estimated_tool_calls >= 0),
            decision TEXT NOT NULL CHECK(decision IN ('allowed','blocked')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id,stage_key)
        );
        CREATE TABLE execution_retry_records(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            retry_number INTEGER NOT NULL CHECK(retry_number > 0),
            reason TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('allowed','blocked')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id,retry_number)
        );
        CREATE INDEX idx_execution_trace_links ON execution_trace_links(trace_id,entity_type,id);
        CREATE TRIGGER execution_trace_scope_immutable
        BEFORE UPDATE OF correlation_root,task_id,run_id,max_tokens,max_cost_usd,
                         max_stages,max_retries,max_tool_calls
        ON execution_traces
        BEGIN SELECT RAISE(ABORT, 'execution trace scope is immutable'); END;
        CREATE TRIGGER execution_trace_terminal_no_update BEFORE UPDATE ON execution_traces
        WHEN OLD.status IN ('completed','failed')
        BEGIN SELECT RAISE(ABORT, 'terminal execution trace is immutable'); END;
        CREATE TRIGGER execution_trace_links_no_update BEFORE UPDATE ON execution_trace_links
        BEGIN SELECT RAISE(ABORT, 'execution trace link is immutable'); END;
        CREATE TRIGGER execution_usage_no_update BEFORE UPDATE ON execution_usage_samples
        BEGIN SELECT RAISE(ABORT, 'execution usage sample is immutable'); END;
        CREATE TRIGGER execution_reservations_no_update BEFORE UPDATE ON execution_stage_reservations
        BEGIN SELECT RAISE(ABORT, 'execution stage reservation is immutable'); END;
        CREATE TRIGGER execution_retries_no_update BEFORE UPDATE ON execution_retry_records
        BEGIN SELECT RAISE(ABORT, 'execution retry record is immutable'); END;
        CREATE TRIGGER execution_traces_no_delete BEFORE DELETE ON execution_traces
        BEGIN SELECT RAISE(ABORT, 'execution trace history is immutable'); END;
        CREATE TRIGGER execution_trace_links_no_delete BEFORE DELETE ON execution_trace_links
        BEGIN SELECT RAISE(ABORT, 'execution trace link is immutable'); END;
        CREATE TRIGGER execution_usage_no_delete BEFORE DELETE ON execution_usage_samples
        BEGIN SELECT RAISE(ABORT, 'execution usage sample is immutable'); END;
        CREATE TRIGGER execution_reservations_no_delete BEFORE DELETE ON execution_stage_reservations
        BEGIN SELECT RAISE(ABORT, 'execution stage reservation is immutable'); END;
        CREATE TRIGGER execution_retries_no_delete BEFORE DELETE ON execution_retry_records
        BEGIN SELECT RAISE(ABORT, 'execution retry record is immutable'); END;
    """),
    (28, """
        CREATE TABLE recovery_inspections(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER REFERENCES workflow_runs(id),
            snapshot_json TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL UNIQUE CHECK(length(snapshot_digest)=64),
            integrity_json TEXT NOT NULL,
            orphan_provider_processes_json TEXT NOT NULL,
            orphan_hermes_sessions_json TEXT NOT NULL,
            orphan_worktrees_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER recovery_inspections_no_update BEFORE UPDATE ON recovery_inspections
        BEGIN SELECT RAISE(ABORT, 'recovery inspection is immutable'); END;
        CREATE TRIGGER recovery_inspections_no_delete BEFORE DELETE ON recovery_inspections
        BEGIN SELECT RAISE(ABORT, 'recovery inspection is immutable'); END;
    """),
    (29, """
        CREATE TABLE hermes_qualification_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            worker_qualification_id INTEGER NOT NULL UNIQUE REFERENCES worker_qualifications(id),
            worker_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            checks_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE CHECK(length(evidence_digest)=64),
            status TEXT NOT NULL CHECK(status IN ('qualified','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER hermes_qualification_runs_no_update BEFORE UPDATE ON hermes_qualification_runs
        BEGIN SELECT RAISE(ABORT, 'Hermes qualification run is immutable'); END;
        CREATE TRIGGER hermes_qualification_runs_no_delete BEFORE DELETE ON hermes_qualification_runs
        BEGIN SELECT RAISE(ABORT, 'Hermes qualification run is immutable'); END;

        CREATE TABLE runtime_fallback_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            source_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            target_qualification_id INTEGER NOT NULL REFERENCES worker_qualifications(id),
            target_worker_id TEXT NOT NULL,
            target_runtime TEXT NOT NULL CHECK(target_runtime IN ('codex-cli','claude-cli')),
            required_capabilities_json TEXT NOT NULL,
            read_only INTEGER NOT NULL CHECK(read_only=1),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER runtime_fallback_authorizations_no_update BEFORE UPDATE ON runtime_fallback_authorizations
        BEGIN SELECT RAISE(ABORT, 'runtime fallback authorization is immutable'); END;
        CREATE TRIGGER runtime_fallback_authorizations_no_delete BEFORE DELETE ON runtime_fallback_authorizations
        BEGIN SELECT RAISE(ABORT, 'runtime fallback authorization is immutable'); END;

        CREATE TABLE runtime_transfer_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            source_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            checkpoint_id INTEGER NOT NULL REFERENCES stage_checkpoints(id),
            target_assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            target_lease_id INTEGER NOT NULL REFERENCES leases(id),
            target_fencing_token INTEGER NOT NULL CHECK(target_fencing_token>0),
            target_runtime TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER runtime_transfer_authorizations_no_update BEFORE UPDATE ON runtime_transfer_authorizations
        BEGIN SELECT RAISE(ABORT, 'runtime transfer authorization is immutable'); END;
        CREATE TRIGGER runtime_transfer_authorizations_no_delete BEFORE DELETE ON runtime_transfer_authorizations
        BEGIN SELECT RAISE(ABORT, 'runtime transfer authorization is immutable'); END;
    """),
    (30, """
        CREATE TABLE claude_worker_results(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            worker_session_id INTEGER NOT NULL UNIQUE REFERENCES worker_sessions(id),
            approval_consumption_id INTEGER NOT NULL UNIQUE REFERENCES stage_approval_consumptions(id),
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            attempt_id INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
            context_package_id INTEGER NOT NULL REFERENCES execution_context_packages(id),
            claude_version TEXT NOT NULL,
            producer_model TEXT NOT NULL,
            qualification_json TEXT NOT NULL,
            permission_profile_json TEXT NOT NULL,
            invocation_json TEXT NOT NULL,
            tool_calls_json TEXT NOT NULL,
            changed_files_json TEXT NOT NULL,
            diff_digest TEXT NOT NULL CHECK(length(diff_digest)=64),
            status TEXT NOT NULL CHECK(status IN ('succeeded','failed','timed_out','cancelled','output_limited')),
            exit_code INTEGER,
            handoff_json TEXT NOT NULL,
            evidence_directory TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE CHECK(length(evidence_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER claude_worker_results_no_update BEFORE UPDATE ON claude_worker_results
        BEGIN SELECT RAISE(ABORT, 'Claude worker result is immutable'); END;
        CREATE TRIGGER claude_worker_results_no_delete BEFORE DELETE ON claude_worker_results
        BEGIN SELECT RAISE(ABORT, 'Claude worker result is immutable'); END;
    """),
    (31, """
        CREATE TABLE role_definitions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            role_id TEXT NOT NULL,
            version TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            contract_digest TEXT NOT NULL UNIQUE CHECK(length(contract_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(role_id,version)
        );
        CREATE TRIGGER role_definitions_no_update BEFORE UPDATE ON role_definitions
        BEGIN SELECT RAISE(ABORT, 'role definition is immutable'); END;
        CREATE TRIGGER role_definitions_no_delete BEFORE DELETE ON role_definitions
        BEGIN SELECT RAISE(ABORT, 'role definition is immutable'); END;

        CREATE TABLE workflow_role_requirements(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL,
            workflow_version TEXT NOT NULL,
            stage_key TEXT NOT NULL,
            role_id TEXT NOT NULL,
            role_version TEXT NOT NULL,
            requirement_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(workflow_id,workflow_version,stage_key),
            FOREIGN KEY(role_id,role_version) REFERENCES role_definitions(role_id,version)
        );
        CREATE TRIGGER workflow_role_requirements_no_update BEFORE UPDATE ON workflow_role_requirements
        BEGIN SELECT RAISE(ABORT, 'workflow role requirement is immutable'); END;
        CREATE TRIGGER workflow_role_requirements_no_delete BEFORE DELETE ON workflow_role_requirements
        BEGIN SELECT RAISE(ABORT, 'workflow role requirement is immutable'); END;

        CREATE TABLE role_decision_assignments(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            decision_key TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            role_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(decision_key,agent_id,role_id,role_version),
            FOREIGN KEY(role_id,role_version) REFERENCES role_definitions(role_id,version)
        );
        CREATE TRIGGER role_decision_assignments_no_update BEFORE UPDATE ON role_decision_assignments
        BEGIN SELECT RAISE(ABORT, 'role decision assignment is immutable'); END;
        CREATE TRIGGER role_decision_assignments_no_delete BEFORE DELETE ON role_decision_assignments
        BEGIN SELECT RAISE(ABORT, 'role decision assignment is immutable'); END;
    """),
    (32, """
        CREATE TABLE agent_routing_decisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            decision_key TEXT NOT NULL UNIQUE,
            role_id TEXT NOT NULL,
            role_version TEXT NOT NULL,
            strategy TEXT NOT NULL CHECK(strategy IN (
                'pinned','best-qualified','cost-aware','latency-aware',
                'diversity','canary','tournament','fallback'
            )),
            request_json TEXT NOT NULL,
            eligible_json TEXT NOT NULL,
            excluded_json TEXT NOT NULL,
            selected_agent_id TEXT NOT NULL,
            fallback_chain_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            decision_digest TEXT NOT NULL UNIQUE CHECK(length(decision_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(role_id,role_version) REFERENCES role_definitions(role_id,version)
        );
        CREATE TRIGGER agent_routing_decisions_no_update BEFORE UPDATE ON agent_routing_decisions
        BEGIN SELECT RAISE(ABORT, 'agent routing decision is immutable'); END;
        CREATE TRIGGER agent_routing_decisions_no_delete BEFORE DELETE ON agent_routing_decisions
        BEGIN SELECT RAISE(ABORT, 'agent routing decision is immutable'); END;
    """),
    (33, """
        CREATE TABLE software_role_packs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_id TEXT NOT NULL,
            version TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE CHECK(length(manifest_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pack_id,version)
        );
        CREATE TRIGGER software_role_packs_no_update BEFORE UPDATE ON software_role_packs
        BEGIN SELECT RAISE(ABORT, 'software role pack is immutable'); END;
        CREATE TRIGGER software_role_packs_no_delete BEFORE DELETE ON software_role_packs
        BEGIN SELECT RAISE(ABORT, 'software role pack is immutable'); END;

        CREATE TABLE release_candidate_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_id INTEGER NOT NULL REFERENCES software_role_packs(id),
            candidate_id INTEGER NOT NULL REFERENCES candidate_change_artifacts(id),
            delivery_id INTEGER NOT NULL REFERENCES coding_delivery_runs(id),
            founder_gate_id INTEGER NOT NULL REFERENCES approval_gates(id),
            github_plan_id INTEGER NOT NULL REFERENCES github_mutation_plans(id),
            release_agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pack_id,candidate_id,release_agent_id)
        );
        CREATE TRIGGER release_candidate_authorizations_no_update BEFORE UPDATE ON release_candidate_authorizations
        BEGIN SELECT RAISE(ABORT, 'release candidate authorization is immutable'); END;
        CREATE TRIGGER release_candidate_authorizations_no_delete BEFORE DELETE ON release_candidate_authorizations
        BEGIN SELECT RAISE(ABORT, 'release candidate authorization is immutable'); END;
    """),
    (34, """
        CREATE TABLE mission_intakes(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            project_id INTEGER NOT NULL REFERENCES projects(id),
            mission_owner TEXT NOT NULL,
            intent TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            intake_digest TEXT NOT NULL UNIQUE CHECK(length(intake_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER mission_intakes_no_update BEFORE UPDATE ON mission_intakes
        BEGIN SELECT RAISE(ABORT, 'mission intake is immutable'); END;
        CREATE TRIGGER mission_intakes_no_delete BEFORE DELETE ON mission_intakes
        BEGIN SELECT RAISE(ABORT, 'mission intake is immutable'); END;

        CREATE TABLE mission_sources(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            intake_id INTEGER NOT NULL REFERENCES mission_intakes(id),
            source_key TEXT NOT NULL,
            subject TEXT NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('authoritative','advisory','reference')),
            version TEXT NOT NULL,
            provenance TEXT NOT NULL,
            content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
            conflict_status TEXT NOT NULL CHECK(conflict_status IN ('clear','conflicted','superseded')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(intake_id,source_key)
        );
        CREATE TRIGGER mission_sources_no_update BEFORE UPDATE ON mission_sources
        BEGIN SELECT RAISE(ABORT, 'mission source is immutable'); END;
        CREATE TRIGGER mission_sources_no_delete BEFORE DELETE ON mission_sources
        BEGIN SELECT RAISE(ABORT, 'mission source is immutable'); END;

        CREATE TABLE mission_owner_resolutions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            intake_id INTEGER NOT NULL REFERENCES mission_intakes(id),
            gap_code TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT NOT NULL,
            resolved_by TEXT NOT NULL,
            accepted_reduced_scope INTEGER NOT NULL DEFAULT 0 CHECK(accepted_reduced_scope IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(intake_id,gap_code)
        );
        CREATE TRIGGER mission_owner_resolutions_no_update BEFORE UPDATE ON mission_owner_resolutions
        BEGIN SELECT RAISE(ABORT, 'mission owner resolution is immutable'); END;
        CREATE TRIGGER mission_owner_resolutions_no_delete BEFORE DELETE ON mission_owner_resolutions
        BEGIN SELECT RAISE(ABORT, 'mission owner resolution is immutable'); END;

        CREATE TABLE mission_readiness_assessments(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            intake_id INTEGER NOT NULL REFERENCES mission_intakes(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            verdict TEXT NOT NULL CHECK(verdict IN (
                'READY_FOR_BLUEPRINT','NEEDS_CLARIFICATION',
                'NEEDS_HUMAN_REVIEW','INFEASIBLE'
            )),
            rationale_json TEXT NOT NULL,
            blocking_gaps_json TEXT NOT NULL,
            assessment_digest TEXT NOT NULL UNIQUE CHECK(length(assessment_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(intake_id,sequence)
        );
        CREATE TRIGGER mission_readiness_assessments_no_update BEFORE UPDATE ON mission_readiness_assessments
        BEGIN SELECT RAISE(ABORT, 'mission readiness assessment is immutable'); END;
        CREATE TRIGGER mission_readiness_assessments_no_delete BEFORE DELETE ON mission_readiness_assessments
        BEGIN SELECT RAISE(ABORT, 'mission readiness assessment is immutable'); END;

        CREATE TABLE mission_review_requests(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            intake_id INTEGER NOT NULL REFERENCES mission_intakes(id),
            assessment_id INTEGER NOT NULL REFERENCES mission_readiness_assessments(id),
            gap_code TEXT NOT NULL,
            request_type TEXT NOT NULL CHECK(request_type IN ('clarification','risk_review','scope_review')),
            prompt TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(assessment_id,gap_code)
        );
        CREATE TRIGGER mission_review_requests_no_update BEFORE UPDATE ON mission_review_requests
        BEGIN SELECT RAISE(ABORT, 'mission review request is immutable'); END;
        CREATE TRIGGER mission_review_requests_no_delete BEFORE DELETE ON mission_review_requests
        BEGIN SELECT RAISE(ABORT, 'mission review request is immutable'); END;
    """),
    (35, """
        CREATE TABLE workforce_exception_reviews(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            review_key TEXT NOT NULL UNIQUE,
            mission_key TEXT NOT NULL,
            pool_key TEXT NOT NULL,
            constraint_key TEXT NOT NULL CHECK(constraint_key IN (
                'model_independence','provider_diversity'
            )),
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            reviewer TEXT NOT NULL,
            reviewer_role TEXT NOT NULL CHECK(reviewer_role IN ('mission_owner','human_reviewer')),
            rationale TEXT NOT NULL,
            review_digest TEXT NOT NULL UNIQUE CHECK(length(review_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER workforce_exception_reviews_no_update BEFORE UPDATE ON workforce_exception_reviews
        BEGIN SELECT RAISE(ABORT, 'workforce exception review is immutable'); END;
        CREATE TRIGGER workforce_exception_reviews_no_delete BEFORE DELETE ON workforce_exception_reviews
        BEGIN SELECT RAISE(ABORT, 'workforce exception review is immutable'); END;

        CREATE TABLE workforce_compositions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            composition_key TEXT NOT NULL UNIQUE,
            mission_key TEXT NOT NULL,
            budget REAL NOT NULL CHECK(budget>=0),
            request_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready','blocked')),
            rationale_json TEXT NOT NULL,
            gaps_json TEXT NOT NULL,
            composition_digest TEXT NOT NULL UNIQUE CHECK(length(composition_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER workforce_compositions_no_update BEFORE UPDATE ON workforce_compositions
        BEGIN SELECT RAISE(ABORT, 'workforce composition is immutable'); END;
        CREATE TRIGGER workforce_compositions_no_delete BEFORE DELETE ON workforce_compositions
        BEGIN SELECT RAISE(ABORT, 'workforce composition is immutable'); END;

        CREATE TABLE workforce_role_pools(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            composition_id INTEGER NOT NULL REFERENCES workforce_compositions(id),
            pool_key TEXT NOT NULL,
            role_id TEXT NOT NULL,
            role_version TEXT NOT NULL,
            qualification_role TEXT NOT NULL,
            pool_strategy TEXT NOT NULL CHECK(pool_strategy IN (
                'singleton','fixed','elastic','strengthened'
            )),
            routing_strategy TEXT NOT NULL CHECK(routing_strategy IN (
                'pinned','best-qualified','cost-aware','latency-aware',
                'diversity','canary','tournament','fallback'
            )),
            minimum_replicas INTEGER NOT NULL CHECK(minimum_replicas>0),
            maximum_replicas INTEGER NOT NULL CHECK(maximum_replicas>=minimum_replicas),
            qualifications_json TEXT NOT NULL,
            arbitration_rule TEXT NOT NULL CHECK(arbitration_rule IN (
                'single','majority','unanimous','ranked_choice','human_decision'
            )),
            constraints_json TEXT NOT NULL,
            primary_assignments_json TEXT NOT NULL,
            fallback_assignments_json TEXT NOT NULL,
            routing_decision_id INTEGER REFERENCES agent_routing_decisions(id),
            estimated_cost REAL NOT NULL CHECK(estimated_cost>=0),
            valid INTEGER NOT NULL CHECK(valid IN (0,1)),
            applied_exceptions_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(composition_id,pool_key),
            FOREIGN KEY(role_id,role_version) REFERENCES role_definitions(role_id,version)
        );
        CREATE TRIGGER workforce_role_pools_no_update BEFORE UPDATE ON workforce_role_pools
        BEGIN SELECT RAISE(ABORT, 'workforce role pool is immutable'); END;
        CREATE TRIGGER workforce_role_pools_no_delete BEFORE DELETE ON workforce_role_pools
        BEGIN SELECT RAISE(ABORT, 'workforce role pool is immutable'); END;
    """),
    (36, """
        CREATE TABLE factory_blueprints(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_key TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            intake_id INTEGER NOT NULL REFERENCES mission_intakes(id),
            readiness_assessment_id INTEGER NOT NULL REFERENCES mission_readiness_assessments(id),
            composition_id INTEGER NOT NULL REFERENCES workforce_compositions(id),
            parent_blueprint_id INTEGER REFERENCES factory_blueprints(id),
            sections_json TEXT NOT NULL,
            trace_json TEXT NOT NULL,
            amendment_impact_json TEXT,
            blueprint_digest TEXT NOT NULL UNIQUE CHECK(length(blueprint_digest)=64),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(blueprint_key,version)
        );
        CREATE TRIGGER factory_blueprints_no_update BEFORE UPDATE ON factory_blueprints
        BEGIN SELECT RAISE(ABORT, 'factory blueprint is immutable'); END;
        CREATE TRIGGER factory_blueprints_no_delete BEFORE DELETE ON factory_blueprints
        BEGIN SELECT RAISE(ABORT, 'factory blueprint is immutable'); END;

        CREATE TABLE blueprint_approvals(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_id INTEGER NOT NULL UNIQUE REFERENCES factory_blueprints(id),
            blueprint_version INTEGER NOT NULL CHECK(blueprint_version>0),
            blueprint_digest TEXT NOT NULL CHECK(length(blueprint_digest)=64),
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            signer TEXT NOT NULL,
            signer_role TEXT NOT NULL CHECK(signer_role='mission_owner'),
            note TEXT NOT NULL,
            approval_digest TEXT NOT NULL UNIQUE CHECK(length(approval_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER blueprint_approvals_no_update BEFORE UPDATE ON blueprint_approvals
        BEGIN SELECT RAISE(ABORT, 'blueprint approval is immutable'); END;
        CREATE TRIGGER blueprint_approvals_no_delete BEFORE DELETE ON blueprint_approvals
        BEGIN SELECT RAISE(ABORT, 'blueprint approval is immutable'); END;

        CREATE TABLE blueprint_execution_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_id INTEGER NOT NULL UNIQUE REFERENCES factory_blueprints(id),
            approval_id INTEGER NOT NULL UNIQUE REFERENCES blueprint_approvals(id),
            blueprint_version INTEGER NOT NULL CHECK(blueprint_version>0),
            blueprint_digest TEXT NOT NULL CHECK(length(blueprint_digest)=64),
            authorization_digest TEXT NOT NULL UNIQUE CHECK(length(authorization_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER blueprint_execution_authorizations_no_update
        BEFORE UPDATE ON blueprint_execution_authorizations
        BEGIN SELECT RAISE(ABORT, 'blueprint execution authorization is immutable'); END;
        CREATE TRIGGER blueprint_execution_authorizations_no_delete
        BEFORE DELETE ON blueprint_execution_authorizations
        BEGIN SELECT RAISE(ABORT, 'blueprint execution authorization is immutable'); END;
    """),
    (37, """
        CREATE TABLE mission_bootstrap_rollback_points(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_id INTEGER NOT NULL REFERENCES factory_blueprints(id),
            blueprint_digest TEXT NOT NULL CHECK(length(blueprint_digest)=64),
            state_json TEXT NOT NULL,
            state_digest TEXT NOT NULL CHECK(length(state_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER mission_bootstrap_rollback_points_no_update
        BEFORE UPDATE ON mission_bootstrap_rollback_points
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap rollback point is immutable'); END;
        CREATE TRIGGER mission_bootstrap_rollback_points_no_delete
        BEFORE DELETE ON mission_bootstrap_rollback_points
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap rollback point is immutable'); END;

        CREATE TABLE mission_bootstrap_attempts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_id INTEGER NOT NULL REFERENCES factory_blueprints(id),
            blueprint_digest TEXT NOT NULL CHECK(length(blueprint_digest)=64),
            rollback_point_id INTEGER NOT NULL REFERENCES mission_bootstrap_rollback_points(id),
            request_json TEXT NOT NULL,
            request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER mission_bootstrap_attempts_no_update BEFORE UPDATE ON mission_bootstrap_attempts
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap attempt is immutable'); END;
        CREATE TRIGGER mission_bootstrap_attempts_no_delete BEFORE DELETE ON mission_bootstrap_attempts
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap attempt is immutable'); END;

        CREATE TABLE bootstrapped_missions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            blueprint_id INTEGER NOT NULL UNIQUE REFERENCES factory_blueprints(id),
            blueprint_digest TEXT NOT NULL UNIQUE CHECK(length(blueprint_digest)=64),
            bootstrap_attempt_id INTEGER NOT NULL UNIQUE REFERENCES mission_bootstrap_attempts(id),
            project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id),
            task_id INTEGER NOT NULL UNIQUE REFERENCES work_items(id),
            workflow_run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
            status TEXT NOT NULL CHECK(status='ready'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER bootstrapped_missions_no_update BEFORE UPDATE ON bootstrapped_missions
        BEGIN SELECT RAISE(ABORT, 'bootstrapped mission is immutable'); END;
        CREATE TRIGGER bootstrapped_missions_no_delete BEFORE DELETE ON bootstrapped_missions
        BEGIN SELECT RAISE(ABORT, 'bootstrapped mission is immutable'); END;

        CREATE TABLE mission_manifests(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES bootstrapped_missions(id),
            manifest_kind TEXT NOT NULL CHECK(manifest_kind IN (
                'agent','role','tool','policy','context','budget','environment'
            )),
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE CHECK(length(manifest_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,manifest_kind)
        );
        CREATE TRIGGER mission_manifests_no_update BEFORE UPDATE ON mission_manifests
        BEGIN SELECT RAISE(ABORT, 'mission manifest is immutable'); END;
        CREATE TRIGGER mission_manifests_no_delete BEFORE DELETE ON mission_manifests
        BEGIN SELECT RAISE(ABORT, 'mission manifest is immutable'); END;

        CREATE TABLE mission_initial_checkpoints(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL UNIQUE REFERENCES bootstrapped_missions(id),
            workflow_run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            state_json TEXT NOT NULL,
            state_digest TEXT NOT NULL UNIQUE CHECK(length(state_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER mission_initial_checkpoints_no_update BEFORE UPDATE ON mission_initial_checkpoints
        BEGIN SELECT RAISE(ABORT, 'mission initial checkpoint is immutable'); END;
        CREATE TRIGGER mission_initial_checkpoints_no_delete BEFORE DELETE ON mission_initial_checkpoints
        BEGIN SELECT RAISE(ABORT, 'mission initial checkpoint is immutable'); END;

        CREATE TABLE mission_bootstrap_outcomes(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            attempt_id INTEGER NOT NULL UNIQUE REFERENCES mission_bootstrap_attempts(id),
            status TEXT NOT NULL CHECK(status IN ('succeeded','failed')),
            mission_id INTEGER REFERENCES bootstrapped_missions(id),
            compensation_json TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER mission_bootstrap_outcomes_no_update BEFORE UPDATE ON mission_bootstrap_outcomes
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap outcome is immutable'); END;
        CREATE TRIGGER mission_bootstrap_outcomes_no_delete BEFORE DELETE ON mission_bootstrap_outcomes
        BEGIN SELECT RAISE(ABORT, 'mission bootstrap outcome is immutable'); END;
    """),
    (38, """
        CREATE TABLE context_compactions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            role_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            source_transcript_digest TEXT NOT NULL CHECK(length(source_transcript_digest)=64),
            retained_state_json TEXT NOT NULL,
            retained_state_digest TEXT NOT NULL UNIQUE CHECK(length(retained_state_digest)=64),
            removed_byte_count INTEGER NOT NULL CHECK(removed_byte_count>=0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER context_compactions_no_update BEFORE UPDATE ON context_compactions
        BEGIN SELECT RAISE(ABORT, 'context compaction is immutable'); END;
        CREATE TRIGGER context_compactions_no_delete BEFORE DELETE ON context_compactions
        BEGIN SELECT RAISE(ABORT, 'context compaction is immutable'); END;

        CREATE TABLE context_broker_dispatches(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL REFERENCES work_items(id),
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            role_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            context_package_id INTEGER NOT NULL UNIQUE REFERENCES execution_context_packages(id),
            context_digest TEXT NOT NULL UNIQUE CHECK(length(context_digest)=64),
            broker_json TEXT NOT NULL,
            broker_digest TEXT NOT NULL UNIQUE CHECK(length(broker_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER context_broker_dispatches_no_update BEFORE UPDATE ON context_broker_dispatches
        BEGIN SELECT RAISE(ABORT, 'context broker dispatch is immutable'); END;
        CREATE TRIGGER context_broker_dispatches_no_delete BEFORE DELETE ON context_broker_dispatches
        BEGIN SELECT RAISE(ABORT, 'context broker dispatch is immutable'); END;
    """),
    (39, """
        CREATE TABLE memory_entries(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            store_type TEXT NOT NULL CHECK(store_type IN (
                'working','semantic','episodic','procedural','entity','contextual','preference','raw_history'
            )),
            memory_type TEXT NOT NULL CHECK(memory_type IN (
                'fact','decision','procedure','outcome','entity','context','preference','raw_event'
            )),
            tenant_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            task_id TEXT,
            purpose TEXT NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('authoritative','verified','advisory','raw')),
            source TEXT NOT NULL,
            source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
            confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            invalidation_conditions_json TEXT NOT NULL,
            content_json TEXT NOT NULL,
            content_digest TEXT NOT NULL UNIQUE CHECK(length(content_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_memory_retrieval
            ON memory_entries(tenant_id,mission_id,purpose,store_type,authority,valid_from,id);
        CREATE TRIGGER memory_entries_no_update BEFORE UPDATE ON memory_entries
        BEGIN SELECT RAISE(ABORT, 'memory entry is immutable'); END;
        CREATE TRIGGER memory_entries_no_delete BEFORE DELETE ON memory_entries
        BEGIN SELECT RAISE(ABORT, 'memory entry is immutable'); END;

        CREATE TABLE memory_consumers(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            memory_id INTEGER NOT NULL REFERENCES memory_entries(id),
            consumer_type TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(memory_id,consumer_type,consumer_id,purpose)
        );
        CREATE TRIGGER memory_consumers_no_update BEFORE UPDATE ON memory_consumers
        BEGIN SELECT RAISE(ABORT, 'memory consumer is immutable'); END;
        CREATE TRIGGER memory_consumers_no_delete BEFORE DELETE ON memory_consumers
        BEGIN SELECT RAISE(ABORT, 'memory consumer is immutable'); END;

        CREATE TABLE memory_invalidations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            memory_id INTEGER NOT NULL UNIQUE REFERENCES memory_entries(id),
            reason TEXT NOT NULL,
            condition_key TEXT NOT NULL,
            invalidated_by TEXT NOT NULL,
            replacement_memory_id INTEGER REFERENCES memory_entries(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER memory_invalidations_no_update BEFORE UPDATE ON memory_invalidations
        BEGIN SELECT RAISE(ABORT, 'memory invalidation is immutable'); END;
        CREATE TRIGGER memory_invalidations_no_delete BEFORE DELETE ON memory_invalidations
        BEGIN SELECT RAISE(ABORT, 'memory invalidation is immutable'); END;

        CREATE TABLE governed_skills(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            skill_key TEXT NOT NULL,
            version TEXT NOT NULL,
            source_memory_id INTEGER REFERENCES memory_entries(id),
            specification_json TEXT NOT NULL,
            specification_digest TEXT NOT NULL UNIQUE CHECK(length(specification_digest)=64),
            status TEXT NOT NULL CHECK(status IN ('draft','approved','deprecated','revoked')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(skill_key,version)
        );
        CREATE TRIGGER governed_skills_no_delete BEFORE DELETE ON governed_skills
        BEGIN SELECT RAISE(ABORT, 'governed skill history is immutable'); END;
        CREATE TRIGGER governed_skills_scope_immutable
        BEFORE UPDATE OF skill_key,version,source_memory_id,specification_json,
                         specification_digest,created_by ON governed_skills
        BEGIN SELECT RAISE(ABORT, 'governed skill definition is immutable'); END;

        CREATE TABLE governed_skill_reviews(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            skill_id INTEGER NOT NULL REFERENCES governed_skills(id),
            tests_version TEXT NOT NULL,
            tests_passed INTEGER NOT NULL CHECK(tests_passed IN (0,1)),
            security_review TEXT NOT NULL CHECK(security_review IN ('passed','failed')),
            evaluation_score REAL NOT NULL CHECK(evaluation_score>=0 AND evaluation_score<=1),
            evaluation_threshold REAL NOT NULL CHECK(evaluation_threshold>=0 AND evaluation_threshold<=1),
            representative_cases INTEGER NOT NULL CHECK(representative_cases>0),
            reviewer TEXT NOT NULL,
            reviewer_role TEXT NOT NULL CHECK(reviewer_role IN ('curator','human_approver')),
            evidence_json TEXT NOT NULL,
            review_digest TEXT NOT NULL UNIQUE CHECK(length(review_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER governed_skill_reviews_no_update BEFORE UPDATE ON governed_skill_reviews
        BEGIN SELECT RAISE(ABORT, 'governed skill review is immutable'); END;
        CREATE TRIGGER governed_skill_reviews_no_delete BEFORE DELETE ON governed_skill_reviews
        BEGIN SELECT RAISE(ABORT, 'governed skill review is immutable'); END;

        CREATE TABLE governed_skill_transitions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            skill_id INTEGER NOT NULL REFERENCES governed_skills(id),
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            review_id INTEGER REFERENCES governed_skill_reviews(id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER governed_skill_transitions_no_update BEFORE UPDATE ON governed_skill_transitions
        BEGIN SELECT RAISE(ABORT, 'governed skill transition is immutable'); END;
        CREATE TRIGGER governed_skill_transitions_no_delete BEFORE DELETE ON governed_skill_transitions
        BEGIN SELECT RAISE(ABORT, 'governed skill transition is immutable'); END;
    """),
    (40, """
        CREATE TABLE tool_descriptors(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tool_key TEXT NOT NULL,
            version TEXT NOT NULL,
            connector_key TEXT NOT NULL,
            descriptor_json TEXT NOT NULL,
            descriptor_digest TEXT NOT NULL UNIQUE CHECK(length(descriptor_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tool_key,version)
        );
        CREATE TRIGGER tool_descriptors_no_update BEFORE UPDATE ON tool_descriptors
        BEGIN SELECT RAISE(ABORT, 'tool descriptor is immutable'); END;
        CREATE TRIGGER tool_descriptors_no_delete BEFORE DELETE ON tool_descriptors
        BEGIN SELECT RAISE(ABORT, 'tool descriptor is immutable'); END;

        CREATE TABLE connector_versions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            connector_key TEXT NOT NULL,
            version TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('native','mcp','cli','http')),
            environment TEXT NOT NULL CHECK(environment IN ('development','production')),
            mutation_capable INTEGER NOT NULL CHECK(mutation_capable IN (0,1)),
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE CHECK(length(manifest_digest)=64),
            approved_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(connector_key,version)
        );
        CREATE TRIGGER connector_versions_no_update BEFORE UPDATE ON connector_versions
        BEGIN SELECT RAISE(ABORT, 'connector version is immutable'); END;
        CREATE TRIGGER connector_versions_no_delete BEFORE DELETE ON connector_versions
        BEGIN SELECT RAISE(ABORT, 'connector version is immutable'); END;

        CREATE TABLE connector_instances(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            connector_key TEXT NOT NULL UNIQUE,
            connector_version_id INTEGER NOT NULL REFERENCES connector_versions(id),
            status TEXT NOT NULL CHECK(status IN ('installed','healthy','unhealthy','disabled','removed')),
            health_reason TEXT NOT NULL,
            version_counter INTEGER NOT NULL DEFAULT 1 CHECK(version_counter>0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER connector_instance_scope_immutable
        BEFORE UPDATE OF identity,connector_key ON connector_instances
        BEGIN SELECT RAISE(ABORT, 'connector identity is immutable'); END;
        CREATE TRIGGER connector_instances_no_delete BEFORE DELETE ON connector_instances
        BEGIN SELECT RAISE(ABORT, 'connector lifecycle is immutable'); END;

        CREATE TABLE connector_lifecycle_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            connector_id INTEGER NOT NULL REFERENCES connector_instances(id),
            connector_version_id INTEGER NOT NULL REFERENCES connector_versions(id),
            event_type TEXT NOT NULL CHECK(event_type IN (
                'installed','healthy','health_failed','disabled','upgraded','removed'
            )),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER connector_lifecycle_events_no_update BEFORE UPDATE ON connector_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'connector lifecycle event is immutable'); END;
        CREATE TRIGGER connector_lifecycle_events_no_delete BEFORE DELETE ON connector_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'connector lifecycle event is immutable'); END;

        CREATE TABLE tool_discoveries(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            connector_id INTEGER NOT NULL REFERENCES connector_instances(id),
            mission_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            discovered_json TEXT NOT NULL,
            authorized_json TEXT NOT NULL,
            discovery_digest TEXT NOT NULL UNIQUE CHECK(length(discovery_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER tool_discoveries_no_update BEFORE UPDATE ON tool_discoveries
        BEGIN SELECT RAISE(ABORT, 'tool discovery is immutable'); END;
        CREATE TRIGGER tool_discoveries_no_delete BEFORE DELETE ON tool_discoveries
        BEGIN SELECT RAISE(ABORT, 'tool discovery is immutable'); END;

        CREATE TABLE tool_invocations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tool_descriptor_id INTEGER NOT NULL REFERENCES tool_descriptors(id),
            connector_id INTEGER NOT NULL REFERENCES connector_instances(id),
            mission_id TEXT NOT NULL,
            role_id TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
            outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','failed','denied')),
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE CHECK(length(evidence_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER tool_invocations_no_update BEFORE UPDATE ON tool_invocations
        BEGIN SELECT RAISE(ABORT, 'tool invocation is immutable'); END;
        CREATE TRIGGER tool_invocations_no_delete BEFORE DELETE ON tool_invocations
        BEGIN SELECT RAISE(ABORT, 'tool invocation is immutable'); END;
    """),
    (41, """
        CREATE TABLE credential_issuances(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            handle TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            tool_key TEXT NOT NULL,
            operations_json TEXT NOT NULL,
            environment_key TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            approved_by TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','revoked','expired')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_credentials_scope
            ON credential_issuances(tenant_id,mission_id,tool_key,status,expires_at);
        CREATE TRIGGER credential_issuance_scope_immutable
        BEFORE UPDATE OF identity,handle,tenant_id,mission_id,tool_key,operations_json,
                         environment_key,expires_at,approved_by ON credential_issuances
        BEGIN SELECT RAISE(ABORT, 'credential scope is immutable'); END;
        CREATE TRIGGER credential_issuances_no_delete BEFORE DELETE ON credential_issuances
        BEGIN SELECT RAISE(ABORT, 'credential issuance history is immutable'); END;

        CREATE TABLE credential_lifecycle_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            credential_id INTEGER NOT NULL REFERENCES credential_issuances(id),
            event_type TEXT NOT NULL CHECK(event_type IN ('issued','used','revoked','expired','denied')),
            actor TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER credential_lifecycle_events_no_update
        BEFORE UPDATE ON credential_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'credential lifecycle event is immutable'); END;
        CREATE TRIGGER credential_lifecycle_events_no_delete
        BEFORE DELETE ON credential_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'credential lifecycle event is immutable'); END;

        CREATE TABLE credential_use_evidence(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            credential_id INTEGER NOT NULL REFERENCES credential_issuances(id),
            tool_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
            outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','failed','denied')),
            sanitized_result_json TEXT NOT NULL,
            result_digest TEXT NOT NULL CHECK(length(result_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER credential_use_evidence_no_update BEFORE UPDATE ON credential_use_evidence
        BEGIN SELECT RAISE(ABORT, 'credential use evidence is immutable'); END;
        CREATE TRIGGER credential_use_evidence_no_delete BEFORE DELETE ON credential_use_evidence
        BEGIN SELECT RAISE(ABORT, 'credential use evidence is immutable'); END;
    """),
    (42, """
        CREATE TABLE red_team_cases(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            stable_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            category TEXT NOT NULL CHECK(category IN (
                'indirect_injection','authority_escalation','secret_extraction',
                'tool_abuse','artifact_poisoning','cross_tenant_access'
            )),
            payload TEXT NOT NULL,
            affected_criterion TEXT NOT NULL,
            case_digest TEXT NOT NULL UNIQUE CHECK(length(case_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stable_id,version)
        );
        CREATE TRIGGER red_team_cases_no_update BEFORE UPDATE ON red_team_cases
        BEGIN SELECT RAISE(ABORT, 'red-team cases are immutable'); END;
        CREATE TRIGGER red_team_cases_no_delete BEFORE DELETE ON red_team_cases
        BEGIN SELECT RAISE(ABORT, 'red-team cases are immutable'); END;

        CREATE TABLE security_attempts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            source TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
            affected_criterion TEXT,
            criterion_evidence_id INTEGER REFERENCES criterion_evidence(id),
            outcome TEXT NOT NULL CHECK(outcome IN ('allowed','denied','quarantined')),
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER security_attempts_no_update BEFORE UPDATE ON security_attempts
        BEGIN SELECT RAISE(ABORT, 'security attempts are immutable'); END;
        CREATE TRIGGER security_attempts_no_delete BEFORE DELETE ON security_attempts
        BEGIN SELECT RAISE(ABORT, 'security attempts are immutable'); END;

        CREATE TABLE security_tripwires(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            attempt_id INTEGER NOT NULL REFERENCES security_attempts(id),
            rule_id TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('medium','high','critical')),
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE CHECK(length(evidence_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(attempt_id,rule_id)
        );
        CREATE TRIGGER security_tripwires_no_update BEFORE UPDATE ON security_tripwires
        BEGIN SELECT RAISE(ABORT, 'security tripwires are immutable'); END;
        CREATE TRIGGER security_tripwires_no_delete BEFORE DELETE ON security_tripwires
        BEGIN SELECT RAISE(ABORT, 'security tripwires are immutable'); END;

        CREATE TABLE quarantined_outputs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            attempt_id INTEGER NOT NULL UNIQUE REFERENCES security_attempts(id),
            content TEXT NOT NULL,
            content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
            risk_level TEXT NOT NULL CHECK(risk_level IN ('high','critical')),
            status TEXT NOT NULL DEFAULT 'quarantined'
                CHECK(status IN ('quarantined','released')),
            released_by TEXT,
            release_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            released_at TEXT
        );
        CREATE TRIGGER quarantined_output_scope_immutable
        BEFORE UPDATE OF identity,attempt_id,content,content_digest,risk_level
        ON quarantined_outputs
        BEGIN SELECT RAISE(ABORT, 'quarantined output scope is immutable'); END;
        CREATE TRIGGER quarantined_output_valid_transition
        BEFORE UPDATE OF status ON quarantined_outputs
        WHEN NOT (OLD.status='quarantined' AND NEW.status='released')
        BEGIN SELECT RAISE(ABORT, 'invalid quarantine transition'); END;
        CREATE TRIGGER quarantined_outputs_no_delete BEFORE DELETE ON quarantined_outputs
        BEGIN SELECT RAISE(ABORT, 'quarantine history is immutable'); END;

        CREATE TABLE security_incidents(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            attempt_id INTEGER NOT NULL REFERENCES security_attempts(id),
            incident_type TEXT NOT NULL CHECK(incident_type IN (
                'prompt_injection','evidence_tampering'
            )),
            severity TEXT NOT NULL CHECK(severity IN ('high','critical')),
            actor TEXT NOT NULL,
            affected_criterion TEXT,
            criterion_evidence_id INTEGER REFERENCES criterion_evidence(id),
            detail_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
            closed_by TEXT,
            closure_reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        );
        CREATE TRIGGER security_incident_scope_immutable
        BEFORE UPDATE OF identity,attempt_id,incident_type,severity,actor,
                         affected_criterion,criterion_evidence_id,detail_json
        ON security_incidents
        BEGIN SELECT RAISE(ABORT, 'security incident scope is immutable'); END;
        CREATE TRIGGER security_incident_valid_transition
        BEFORE UPDATE OF status ON security_incidents
        WHEN NOT (OLD.status='open' AND NEW.status='closed')
        BEGIN SELECT RAISE(ABORT, 'invalid security incident transition'); END;
        CREATE TRIGGER security_incidents_no_delete BEFORE DELETE ON security_incidents
        BEGIN SELECT RAISE(ABORT, 'security incident history is immutable'); END;

        CREATE TABLE quarantined_output_admissions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            quarantine_id INTEGER NOT NULL REFERENCES quarantined_outputs(id),
            sink TEXT NOT NULL CHECK(sink IN (
                'accepted_context','memory','artifact','downstream_execution'
            )),
            admitted_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER quarantined_output_admissions_no_update
        BEFORE UPDATE ON quarantined_output_admissions
        BEGIN SELECT RAISE(ABORT, 'quarantine admissions are immutable'); END;
        CREATE TRIGGER quarantined_output_admissions_no_delete
        BEFORE DELETE ON quarantined_output_admissions
        BEGIN SELECT RAISE(ABORT, 'quarantine admissions are immutable'); END;

        CREATE TABLE red_team_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            corpus_digest TEXT NOT NULL CHECK(length(corpus_digest)=64),
            executed_by TEXT NOT NULL,
            total_cases INTEGER NOT NULL CHECK(total_cases>0),
            contained_cases INTEGER NOT NULL CHECK(contained_cases>=0),
            verdict TEXT NOT NULL CHECK(verdict IN ('passed','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER red_team_runs_no_update BEFORE UPDATE ON red_team_runs
        BEGIN SELECT RAISE(ABORT, 'red-team runs are immutable'); END;
        CREATE TRIGGER red_team_runs_no_delete BEFORE DELETE ON red_team_runs
        BEGIN SELECT RAISE(ABORT, 'red-team runs are immutable'); END;

        CREATE TABLE red_team_results(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES red_team_runs(id),
            case_id INTEGER NOT NULL REFERENCES red_team_cases(id),
            attempt_id INTEGER NOT NULL REFERENCES security_attempts(id),
            tripwire_id INTEGER REFERENCES security_tripwires(id),
            quarantine_id INTEGER REFERENCES quarantined_outputs(id),
            incident_id INTEGER REFERENCES security_incidents(id),
            contained INTEGER NOT NULL CHECK(contained IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id,case_id)
        );
        CREATE TRIGGER red_team_results_no_update BEFORE UPDATE ON red_team_results
        BEGIN SELECT RAISE(ABORT, 'red-team results are immutable'); END;
        CREATE TRIGGER red_team_results_no_delete BEFORE DELETE ON red_team_results
        BEGIN SELECT RAISE(ABORT, 'red-team results are immutable'); END;
    """),
    (43, """
        CREATE TABLE coordination_patterns(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pattern_key TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            pattern_type TEXT NOT NULL CHECK(pattern_type IN (
                'parallel','generator_critic','quorum','debate','tournament','red_blue'
            )),
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE CHECK(length(manifest_digest)=64),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pattern_key,version)
        );
        CREATE TRIGGER coordination_patterns_no_update BEFORE UPDATE ON coordination_patterns
        BEGIN SELECT RAISE(ABORT, 'coordination patterns are immutable'); END;
        CREATE TRIGGER coordination_patterns_no_delete BEFORE DELETE ON coordination_patterns
        BEGIN SELECT RAISE(ABORT, 'coordination patterns are immutable'); END;

        CREATE TABLE coordination_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pattern_id INTEGER NOT NULL REFERENCES coordination_patterns(id),
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running','completed','terminated')),
            turn_count INTEGER NOT NULL DEFAULT 0 CHECK(turn_count>=0),
            tokens_used INTEGER NOT NULL DEFAULT 0 CHECK(tokens_used>=0),
            cost_used REAL NOT NULL DEFAULT 0 CHECK(cost_used>=0),
            termination_reason TEXT,
            outcome_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE TRIGGER coordination_run_scope_immutable
        BEFORE UPDATE OF identity,pattern_id,objective ON coordination_runs
        BEGIN SELECT RAISE(ABORT, 'coordination run scope is immutable'); END;
        CREATE TRIGGER coordination_run_valid_transition
        BEFORE UPDATE OF status ON coordination_runs
        WHEN NOT (OLD.status='running' AND NEW.status IN ('completed','terminated'))
        BEGIN SELECT RAISE(ABORT, 'invalid coordination run transition'); END;
        CREATE TRIGGER coordination_runs_no_delete BEFORE DELETE ON coordination_runs
        BEGIN SELECT RAISE(ABORT, 'coordination run history is immutable'); END;

        CREATE TABLE coordination_reviewer_selections(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES coordination_runs(id),
            producer_id TEXT NOT NULL,
            producer_model TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            reviewer_model TEXT NOT NULL,
            eligible_json TEXT NOT NULL,
            excluded_json TEXT NOT NULL,
            strategy TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER coordination_reviewer_selections_no_update
        BEFORE UPDATE ON coordination_reviewer_selections
        BEGIN SELECT RAISE(ABORT, 'coordination reviewer selections are immutable'); END;
        CREATE TRIGGER coordination_reviewer_selections_no_delete
        BEFORE DELETE ON coordination_reviewer_selections
        BEGIN SELECT RAISE(ABORT, 'coordination reviewer selections are immutable'); END;

        CREATE TABLE coordination_contributions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES coordination_runs(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            participant_id TEXT NOT NULL,
            participant_model TEXT NOT NULL,
            participant_role TEXT NOT NULL,
            contribution_type TEXT NOT NULL CHECK(contribution_type IN (
                'proposal','critique','vote','argument','verdict','attack','defense','score'
            )),
            payload_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            dissent_json TEXT NOT NULL,
            reviewer_selection_id INTEGER REFERENCES coordination_reviewer_selections(id),
            tokens_used INTEGER NOT NULL CHECK(tokens_used>=0),
            cost_used REAL NOT NULL CHECK(cost_used>=0),
            contribution_digest TEXT NOT NULL UNIQUE CHECK(length(contribution_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id,sequence)
        );
        CREATE TRIGGER coordination_contributions_no_update
        BEFORE UPDATE ON coordination_contributions
        BEGIN SELECT RAISE(ABORT, 'coordination contributions are immutable'); END;
        CREATE TRIGGER coordination_contributions_no_delete
        BEFORE DELETE ON coordination_contributions
        BEGIN SELECT RAISE(ABORT, 'coordination contributions are immutable'); END;

        CREATE TABLE coordination_arbitrations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL UNIQUE REFERENCES coordination_runs(id),
            strategy TEXT NOT NULL,
            contribution_ids_json TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            dissent_json TEXT NOT NULL,
            arbitration_digest TEXT NOT NULL UNIQUE CHECK(length(arbitration_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER coordination_arbitrations_no_update
        BEFORE UPDATE ON coordination_arbitrations
        BEGIN SELECT RAISE(ABORT, 'coordination arbitrations are immutable'); END;
        CREATE TRIGGER coordination_arbitrations_no_delete
        BEFORE DELETE ON coordination_arbitrations
        BEGIN SELECT RAISE(ABORT, 'coordination arbitrations are immutable'); END;
    """),
    (44, """
        CREATE TABLE architecture_decisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            adr_key TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            blueprint_id INTEGER NOT NULL REFERENCES factory_blueprints(id),
            context TEXT NOT NULL,
            alternatives_json TEXT NOT NULL,
            decision TEXT NOT NULL,
            consequences_json TEXT NOT NULL,
            affected_contracts_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            material_domains_json TEXT NOT NULL,
            architecture_owner TEXT NOT NULL,
            decision_digest TEXT NOT NULL UNIQUE CHECK(length(decision_digest)=64),
            status TEXT NOT NULL DEFAULT 'proposed'
                CHECK(status IN ('proposed','approved','rejected','applied')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(adr_key,version)
        );
        CREATE TRIGGER architecture_decision_scope_immutable
        BEFORE UPDATE OF identity,adr_key,version,blueprint_id,context,alternatives_json,
                         decision,consequences_json,affected_contracts_json,evidence_json,
                         material_domains_json,architecture_owner,decision_digest,created_by
        ON architecture_decisions
        BEGIN SELECT RAISE(ABORT, 'architecture decision scope is immutable'); END;
        CREATE TRIGGER architecture_decision_valid_transition
        BEFORE UPDATE OF status ON architecture_decisions
        WHEN NOT (
            (OLD.status='proposed' AND NEW.status IN ('approved','rejected')) OR
            (OLD.status='approved' AND NEW.status='applied')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid architecture decision transition'); END;
        CREATE TRIGGER architecture_decisions_no_delete BEFORE DELETE ON architecture_decisions
        BEGIN SELECT RAISE(ABORT, 'architecture decisions are immutable'); END;

        CREATE TABLE adr_impact_analyses(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            adr_id INTEGER NOT NULL UNIQUE REFERENCES architecture_decisions(id),
            impact_json TEXT NOT NULL,
            impact_digest TEXT NOT NULL UNIQUE CHECK(length(impact_digest)=64),
            analyzed_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER adr_impact_analyses_no_update BEFORE UPDATE ON adr_impact_analyses
        BEGIN SELECT RAISE(ABORT, 'ADR impact analysis is immutable'); END;
        CREATE TRIGGER adr_impact_analyses_no_delete BEFORE DELETE ON adr_impact_analyses
        BEGIN SELECT RAISE(ABORT, 'ADR impact analysis is immutable'); END;

        CREATE TABLE adr_approvals(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            adr_id INTEGER NOT NULL UNIQUE REFERENCES architecture_decisions(id),
            decision_digest TEXT NOT NULL CHECK(length(decision_digest)=64),
            impact_digest TEXT NOT NULL CHECK(length(impact_digest)=64),
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            reviewer TEXT NOT NULL,
            reviewer_role TEXT NOT NULL CHECK(reviewer_role='human_architecture_owner'),
            note TEXT NOT NULL,
            approval_digest TEXT NOT NULL UNIQUE CHECK(length(approval_digest)=64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER adr_approvals_no_update BEFORE UPDATE ON adr_approvals
        BEGIN SELECT RAISE(ABORT, 'ADR approvals are immutable'); END;
        CREATE TRIGGER adr_approvals_no_delete BEFORE DELETE ON adr_approvals
        BEGIN SELECT RAISE(ABORT, 'ADR approvals are immutable'); END;

        CREATE TABLE adr_applications(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            adr_id INTEGER NOT NULL UNIQUE REFERENCES architecture_decisions(id),
            impact_analysis_id INTEGER NOT NULL UNIQUE REFERENCES adr_impact_analyses(id),
            approval_id INTEGER NOT NULL UNIQUE REFERENCES adr_approvals(id),
            prior_blueprint_id INTEGER NOT NULL REFERENCES factory_blueprints(id),
            new_blueprint_id INTEGER NOT NULL UNIQUE REFERENCES factory_blueprints(id),
            application_digest TEXT NOT NULL UNIQUE CHECK(length(application_digest)=64),
            applied_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER adr_applications_no_update BEFORE UPDATE ON adr_applications
        BEGIN SELECT RAISE(ABORT, 'ADR applications are immutable'); END;
        CREATE TRIGGER adr_applications_no_delete BEFORE DELETE ON adr_applications
        BEGIN SELECT RAISE(ABORT, 'ADR applications are immutable'); END;

        CREATE TABLE adr_contract_propagations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            application_id INTEGER NOT NULL REFERENCES adr_applications(id),
            target_type TEXT NOT NULL CHECK(target_type IN (
                'task','context_package','policy','evaluation','artifact',
                'deployment_assumption','workflow_contract'
            )),
            target_ref TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(application_id,target_type,target_ref)
        );
        CREATE TRIGGER adr_contract_propagations_no_update
        BEFORE UPDATE ON adr_contract_propagations
        BEGIN SELECT RAISE(ABORT, 'ADR propagation records are immutable'); END;
        CREATE TRIGGER adr_contract_propagations_no_delete
        BEFORE DELETE ON adr_contract_propagations
        BEGIN SELECT RAISE(ABORT, 'ADR propagation records are immutable'); END;

        CREATE TABLE adr_workflow_contract_versions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            contract_key TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            application_id INTEGER NOT NULL REFERENCES adr_applications(id),
            contract_json TEXT NOT NULL,
            contract_digest TEXT NOT NULL UNIQUE CHECK(length(contract_digest)=64),
            previous_version_id INTEGER REFERENCES adr_workflow_contract_versions(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contract_key,version)
        );
        CREATE TRIGGER adr_workflow_contract_versions_no_update
        BEFORE UPDATE ON adr_workflow_contract_versions
        BEGIN SELECT RAISE(ABORT, 'ADR workflow contract versions are immutable'); END;
        CREATE TRIGGER adr_workflow_contract_versions_no_delete
        BEFORE DELETE ON adr_workflow_contract_versions
        BEGIN SELECT RAISE(ABORT, 'ADR workflow contract versions are immutable'); END;
    """),
    (45, """
        CREATE TABLE pack_trust_roots(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            key_id TEXT NOT NULL UNIQUE,
            algorithm TEXT NOT NULL CHECK(algorithm='hmac-sha256'),
            key_fingerprint TEXT NOT NULL UNIQUE CHECK(length(key_fingerprint)=64),
            approved_by TEXT NOT NULL,
            approved_role TEXT NOT NULL CHECK(approved_role='human_administrator'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER pack_trust_roots_no_update BEFORE UPDATE ON pack_trust_roots
        BEGIN SELECT RAISE(ABORT, 'pack trust roots are immutable'); END;
        CREATE TRIGGER pack_trust_roots_no_delete BEFORE DELETE ON pack_trust_roots
        BEGIN SELECT RAISE(ABORT, 'pack trust roots are immutable'); END;

        CREATE TABLE pack_versions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_key TEXT NOT NULL,
            version TEXT NOT NULL,
            pack_type TEXT NOT NULL CHECK(pack_type IN (
                'domain','capability','connector','policy','evaluation','ui'
            )),
            manifest_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_digest TEXT NOT NULL UNIQUE CHECK(length(content_digest)=64),
            signature TEXT NOT NULL CHECK(length(signature)=64),
            trust_root_id INTEGER NOT NULL REFERENCES pack_trust_roots(id),
            previous_version_id INTEGER REFERENCES pack_versions(id),
            installed_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pack_key,version)
        );
        CREATE TRIGGER pack_versions_no_update BEFORE UPDATE ON pack_versions
        BEGIN SELECT RAISE(ABORT, 'pack versions are immutable'); END;
        CREATE TRIGGER pack_versions_no_delete BEFORE DELETE ON pack_versions
        BEGIN SELECT RAISE(ABORT, 'pack versions are immutable'); END;

        CREATE TABLE pack_qualifications(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_version_id INTEGER NOT NULL UNIQUE REFERENCES pack_versions(id),
            results_json TEXT NOT NULL,
            qualification_digest TEXT NOT NULL UNIQUE CHECK(length(qualification_digest)=64),
            verdict TEXT NOT NULL CHECK(verdict='passed'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER pack_qualifications_no_update BEFORE UPDATE ON pack_qualifications
        BEGIN SELECT RAISE(ABORT, 'pack qualifications are immutable'); END;
        CREATE TRIGGER pack_qualifications_no_delete BEFORE DELETE ON pack_qualifications
        BEGIN SELECT RAISE(ABORT, 'pack qualifications are immutable'); END;

        CREATE TABLE pack_installations(
            pack_key TEXT PRIMARY KEY,
            active_version_id INTEGER REFERENCES pack_versions(id),
            state TEXT NOT NULL CHECK(state IN ('active','disabled')),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE pack_lifecycle_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_key TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'installed','upgraded','disabled','enabled','rolled_back'
            )),
            from_version_id INTEGER REFERENCES pack_versions(id),
            to_version_id INTEGER REFERENCES pack_versions(id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER pack_lifecycle_events_no_update BEFORE UPDATE ON pack_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'pack lifecycle events are immutable'); END;
        CREATE TRIGGER pack_lifecycle_events_no_delete BEFORE DELETE ON pack_lifecycle_events
        BEGIN SELECT RAISE(ABORT, 'pack lifecycle events are immutable'); END;
    """),
    (46, """
        CREATE TABLE reference_pack_releases(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            pack_version_id INTEGER NOT NULL UNIQUE REFERENCES pack_versions(id),
            role_pack_id INTEGER NOT NULL REFERENCES software_role_packs(id),
            release_manifest_json TEXT NOT NULL,
            release_manifest_digest TEXT NOT NULL UNIQUE CHECK(length(release_manifest_digest)=64),
            dependency_evidence_json TEXT NOT NULL,
            security_evidence_json TEXT NOT NULL,
            rollback_evidence_json TEXT NOT NULL,
            traceability_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate'
                CHECK(status IN ('candidate','approved','published','rolled_back')),
            release_authority TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT,
            published_at TEXT,
            rolled_back_at TEXT
        );
        CREATE TRIGGER reference_pack_releases_scope_immutable
        BEFORE UPDATE OF identity,pack_version_id,role_pack_id,release_manifest_json,
                         release_manifest_digest,dependency_evidence_json,
                         security_evidence_json,rollback_evidence_json,traceability_json,
                         release_authority ON reference_pack_releases
        BEGIN SELECT RAISE(ABORT, 'reference pack release scope is immutable'); END;
        CREATE TRIGGER reference_pack_releases_valid_transition
        BEFORE UPDATE OF status ON reference_pack_releases
        WHEN NOT (
            (OLD.status='candidate' AND NEW.status='approved') OR
            (OLD.status='approved' AND NEW.status='published') OR
            (OLD.status IN ('approved','published') AND NEW.status='rolled_back')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid reference pack release transition'); END;
        CREATE TRIGGER reference_pack_releases_no_delete BEFORE DELETE ON reference_pack_releases
        BEGIN SELECT RAISE(ABORT, 'reference pack release history is immutable'); END;

        CREATE TABLE reference_pack_release_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            release_id INTEGER NOT NULL REFERENCES reference_pack_releases(id),
            event_type TEXT NOT NULL CHECK(event_type IN ('approved','published','rolled_back')),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER reference_pack_release_events_no_update
        BEFORE UPDATE ON reference_pack_release_events
        BEGIN SELECT RAISE(ABORT, 'reference pack release events are immutable'); END;
        CREATE TRIGGER reference_pack_release_events_no_delete
        BEFORE DELETE ON reference_pack_release_events
        BEGIN SELECT RAISE(ABORT, 'reference pack release events are immutable'); END;
    """),
    (47, """
        CREATE TABLE otel_exports(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            correlation_root TEXT NOT NULL,
            exporter TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL UNIQUE CHECK(length(payload_digest)=64),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER otel_exports_no_update BEFORE UPDATE ON otel_exports
        BEGIN SELECT RAISE(ABORT, 'OpenTelemetry exports are immutable'); END;
        CREATE TRIGGER otel_exports_no_delete BEFORE DELETE ON otel_exports
        BEGIN SELECT RAISE(ABORT, 'OpenTelemetry exports are immutable'); END;

        CREATE TABLE cost_ledger_entries(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            idempotency_key TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('provider_reported','estimated')),
            tokens INTEGER NOT NULL CHECK(tokens>=0),
            duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
            cost_usd REAL NOT NULL CHECK(cost_usd>=0),
            currency TEXT NOT NULL DEFAULT 'USD' CHECK(currency='USD'),
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_cost_ledger_trace ON cost_ledger_entries(trace_id,created_at,id);
        CREATE TRIGGER cost_ledger_entries_no_update BEFORE UPDATE ON cost_ledger_entries
        BEGIN SELECT RAISE(ABORT, 'cost ledger entries are immutable'); END;
        CREATE TRIGGER cost_ledger_entries_no_delete BEFORE DELETE ON cost_ledger_entries
        BEGIN SELECT RAISE(ABORT, 'cost ledger entries are immutable'); END;

        CREATE TABLE budget_threshold_policies(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            policy_key TEXT NOT NULL UNIQUE,
            metric TEXT NOT NULL CHECK(metric IN ('cost_usd','tokens','duration_ms')),
            threshold REAL NOT NULL CHECK(threshold>=0),
            action TEXT NOT NULL CHECK(action IN ('notify','reroute','pause','require_approval')),
            hard_budget INTEGER NOT NULL DEFAULT 0 CHECK(hard_budget IN (0,1)),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER budget_threshold_policies_no_update BEFORE UPDATE ON budget_threshold_policies
        BEGIN SELECT RAISE(ABORT, 'budget threshold policies are immutable'); END;
        CREATE TRIGGER budget_threshold_policies_no_delete BEFORE DELETE ON budget_threshold_policies
        BEGIN SELECT RAISE(ABORT, 'budget threshold policies are immutable'); END;

        CREATE TABLE budget_threshold_actions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            policy_id INTEGER NOT NULL REFERENCES budget_threshold_policies(id),
            observed_value REAL NOT NULL CHECK(observed_value>=0),
            action TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('recorded','applied','awaiting_approval')),
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id,policy_id)
        );
        CREATE TRIGGER budget_threshold_actions_no_update BEFORE UPDATE ON budget_threshold_actions
        BEGIN SELECT RAISE(ABORT, 'budget threshold actions are immutable'); END;
        CREATE TRIGGER budget_threshold_actions_no_delete BEFORE DELETE ON budget_threshold_actions
        BEGIN SELECT RAISE(ABORT, 'budget threshold actions are immutable'); END;

        CREATE TABLE budget_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            trace_id INTEGER NOT NULL REFERENCES execution_traces(id),
            previous_max_cost_usd REAL NOT NULL CHECK(previous_max_cost_usd>=0),
            new_max_cost_usd REAL NOT NULL CHECK(new_max_cost_usd>=previous_max_cost_usd),
            authority TEXT NOT NULL,
            authority_role TEXT NOT NULL CHECK(authority_role='human_budget_authority'),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER budget_authorizations_no_update BEFORE UPDATE ON budget_authorizations
        BEGIN SELECT RAISE(ABORT, 'budget authorizations are immutable'); END;
        CREATE TRIGGER budget_authorizations_no_delete BEFORE DELETE ON budget_authorizations
        BEGIN SELECT RAISE(ABORT, 'budget authorizations are immutable'); END;
    """),
    (48, """
        DROP TRIGGER execution_trace_scope_immutable;
        CREATE TRIGGER execution_trace_scope_immutable
        BEFORE UPDATE OF correlation_root,task_id,run_id,max_tokens,
                         max_stages,max_retries,max_tool_calls
        ON execution_traces
        BEGIN SELECT RAISE(ABORT, 'execution trace scope is immutable'); END;
        CREATE TRIGGER execution_trace_budget_authorized
        BEFORE UPDATE OF max_cost_usd ON execution_traces
        WHEN NOT EXISTS(
            SELECT 1 FROM budget_authorizations
             WHERE trace_id=OLD.id
               AND previous_max_cost_usd=OLD.max_cost_usd
               AND new_max_cost_usd=NEW.max_cost_usd
        )
        BEGIN SELECT RAISE(ABORT, 'hard budget increase requires human authorization'); END;
    """),
    (49, """
        CREATE TABLE IF NOT EXISTS tenant_policies(
            tenant_id TEXT PRIMARY KEY,
            classification TEXT NOT NULL CHECK(classification IN ('public','internal','confidential','restricted')),
            residency TEXT NOT NULL,
            retention_seconds INTEGER NOT NULL CHECK(retention_seconds >= 0),
            quota_bytes INTEGER NOT NULL CHECK(quota_bytes > 0),
            legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tenant_objects(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            object_key TEXT NOT NULL,
            digest TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            classification TEXT NOT NULL,
            residency TEXT NOT NULL,
            created_at TEXT NOT NULL,
            retention_until TEXT NOT NULL,
            deleted_at TEXT,
            UNIQUE(tenant_id, object_key)
        );
        CREATE INDEX IF NOT EXISTS idx_tenant_objects_scope ON tenant_objects(tenant_id, deleted_at, id);
        CREATE TABLE IF NOT EXISTS tenant_governance_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tenant_exports(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            verified INTEGER NOT NULL CHECK(verified IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tenant_deletions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            object_identity TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('deleted','blocked')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS tenant_objects_no_update BEFORE UPDATE ON tenant_objects
        WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'tenant objects are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_objects_no_delete BEFORE DELETE ON tenant_objects
        BEGIN SELECT RAISE(ABORT, 'tenant objects are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_governance_events_no_update BEFORE UPDATE ON tenant_governance_events
        BEGIN SELECT RAISE(ABORT, 'tenant governance events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_governance_events_no_delete BEFORE DELETE ON tenant_governance_events
        BEGIN SELECT RAISE(ABORT, 'tenant governance events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_exports_no_update BEFORE UPDATE ON tenant_exports
        BEGIN SELECT RAISE(ABORT, 'tenant exports are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_exports_no_delete BEFORE DELETE ON tenant_exports
        BEGIN SELECT RAISE(ABORT, 'tenant exports are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_deletions_no_update BEFORE UPDATE ON tenant_deletions
        BEGIN SELECT RAISE(ABORT, 'tenant deletion evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tenant_deletions_no_delete BEFORE DELETE ON tenant_deletions
        BEGIN SELECT RAISE(ABORT, 'tenant deletion evidence is immutable'); END;
    """),
    (50, """
        CREATE TABLE IF NOT EXISTS deployment_operations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            profile TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('deploy','upgrade','rollback')),
            from_version TEXT,
            to_version TEXT NOT NULL,
            continuity_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('verified','blocked')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS deployment_operations_no_update BEFORE UPDATE ON deployment_operations
        BEGIN SELECT RAISE(ABORT, 'deployment evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS deployment_operations_no_delete BEFORE DELETE ON deployment_operations
        BEGIN SELECT RAISE(ABORT, 'deployment evidence is immutable'); END;
    """),
    (51, """
        CREATE TABLE IF NOT EXISTS qualification_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            suite_version TEXT NOT NULL,
            environment_json TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            raw_evidence_json TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('passed','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS qualification_runs_no_update BEFORE UPDATE ON qualification_runs
        BEGIN SELECT RAISE(ABORT, 'qualification evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS qualification_runs_no_delete BEFORE DELETE ON qualification_runs
        BEGIN SELECT RAISE(ABORT, 'qualification evidence is immutable'); END;
    """),
    (52, """
        CREATE TABLE IF NOT EXISTS chaos_recovery_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            profile TEXT NOT NULL,
            fault_boundary TEXT NOT NULL,
            identities_json TEXT NOT NULL,
            restore_json TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('passed','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS chaos_recovery_runs_no_update BEFORE UPDATE ON chaos_recovery_runs
        BEGIN SELECT RAISE(ABORT, 'chaos recovery evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS chaos_recovery_runs_no_delete BEFORE DELETE ON chaos_recovery_runs
        BEGIN SELECT RAISE(ABORT, 'chaos recovery evidence is immutable'); END;
    """),
    (53, """
        CREATE TABLE IF NOT EXISTS soak_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            workload_version TEXT NOT NULL,
            fault_schedule_json TEXT NOT NULL,
            duration_hours REAL NOT NULL,
            resource_evidence_json TEXT NOT NULL,
            continuity_json TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('passed','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS soak_runs_no_update BEFORE UPDATE ON soak_runs
        BEGIN SELECT RAISE(ABORT, 'soak evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS soak_runs_no_delete BEFORE DELETE ON soak_runs
        BEGIN SELECT RAISE(ABORT, 'soak evidence is immutable'); END;
    """),
    (54, """
        CREATE TABLE IF NOT EXISTS acceptance_missions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_version TEXT NOT NULL,
            providers_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            release_digest TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('accepted','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS acceptance_missions_no_update BEFORE UPDATE ON acceptance_missions
        BEGIN SELECT RAISE(ABORT, 'acceptance mission evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS acceptance_missions_no_delete BEFORE DELETE ON acceptance_missions
        BEGIN SELECT RAISE(ABORT, 'acceptance mission evidence is immutable'); END;
    """),
    (55, """
        CREATE TABLE IF NOT EXISTS handover_bundles(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            bundle_version TEXT NOT NULL,
            checklist_json TEXT NOT NULL,
            evidence_index_json TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('ready','blocked')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER IF NOT EXISTS handover_bundles_no_update BEFORE UPDATE ON handover_bundles
        BEGIN SELECT RAISE(ABORT, 'handover evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS handover_bundles_no_delete BEFORE DELETE ON handover_bundles
        BEGIN SELECT RAISE(ABORT, 'handover evidence is immutable'); END;
    """),
    (56, """
        CREATE TABLE IF NOT EXISTS api_idempotency(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS api_webhook_deliveries(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            event_json TEXT NOT NULL,
            signature TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id,delivery_key)
        );
        CREATE INDEX IF NOT EXISTS idx_api_idempotency_scope ON api_idempotency(tenant_id,idempotency_key);
        CREATE TRIGGER IF NOT EXISTS api_idempotency_no_update BEFORE UPDATE ON api_idempotency
        BEGIN SELECT RAISE(ABORT, 'API idempotency records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS api_idempotency_no_delete BEFORE DELETE ON api_idempotency
        BEGIN SELECT RAISE(ABORT, 'API idempotency records are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS api_webhooks_no_update BEFORE UPDATE ON api_webhook_deliveries
        WHEN OLD.status='delivered'
        BEGIN SELECT RAISE(ABORT, 'delivered webhooks are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS api_webhooks_no_delete BEFORE DELETE ON api_webhook_deliveries
        BEGIN SELECT RAISE(ABORT, 'webhook delivery evidence is immutable'); END;
    """),
    (57, """
        CREATE TABLE IF NOT EXISTS control_plane_actions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_control_actions_scope ON control_plane_actions(tenant_id,created_at);
        CREATE TRIGGER IF NOT EXISTS control_plane_actions_no_update BEFORE UPDATE ON control_plane_actions
        BEGIN SELECT RAISE(ABORT, 'control-plane actions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS control_plane_actions_no_delete BEFORE DELETE ON control_plane_actions
        BEGIN SELECT RAISE(ABORT, 'control-plane actions are immutable'); END;
    """),
    (58, """
        CREATE TABLE IF NOT EXISTS autonomous_missions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_key TEXT NOT NULL UNIQUE,
            project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id),
            intake_id INTEGER REFERENCES mission_intakes(id),
            blueprint_id INTEGER REFERENCES factory_blueprints(id),
            name TEXT NOT NULL,
            mission_owner TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN (
                'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION',
                'WAITING_FOR_BACKLOG_APPROVAL','APPROVED',
                'ENVIRONMENT_DISCOVERY','ENVIRONMENT_BOOTSTRAP','DEVELOPMENT',
                'VALIDATION','INTEGRATION','FINAL_VALIDATION','COMPLETED'
            )),
            disposition TEXT NOT NULL CHECK(disposition IN (
                'RUNNING','PAUSED','STOPPED','NEEDS_ATTENTION',
                'NEEDS_HUMAN_ACTION','REPLANNING','RECOVERING','FAILED'
            )),
            configuration_json TEXT NOT NULL,
            configuration_digest TEXT NOT NULL,
            initial_specification_text TEXT NOT NULL DEFAULT '',
            initial_specification_digest TEXT,
            specification_metadata_json TEXT NOT NULL DEFAULT '{}',
            active_backlog_revision_id INTEGER,
            active_execution_epoch_id INTEGER,
            current_checkpoint_id INTEGER,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_missions_state
            ON autonomous_missions(disposition,phase,updated_at);

        CREATE TABLE IF NOT EXISTS autonomous_mission_state_versions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            version INTEGER NOT NULL CHECK(version > 0),
            phase TEXT NOT NULL,
            disposition TEXT NOT NULL,
            configuration_json TEXT NOT NULL,
            configuration_digest TEXT NOT NULL,
            active_backlog_revision_id INTEGER,
            active_execution_epoch_id INTEGER,
            current_checkpoint_id INTEGER,
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,version)
        );

        CREATE TABLE IF NOT EXISTS autonomous_mission_commands(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            command_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            expected_version INTEGER,
            request_digest TEXT NOT NULL,
            result_version INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_mission_commands
            ON autonomous_mission_commands(mission_id,id);

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_no_delete
        BEFORE DELETE ON autonomous_missions
        BEGIN SELECT RAISE(ABORT, 'autonomous missions are durable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_identity_immutable
        BEFORE UPDATE ON autonomous_missions
        WHEN NEW.identity<>OLD.identity OR NEW.mission_key<>OLD.mission_key
          OR NEW.project_id<>OLD.project_id OR NEW.mission_owner<>OLD.mission_owner
          OR NEW.initial_specification_text<>OLD.initial_specification_text
          OR COALESCE(NEW.initial_specification_digest,'')<>
             COALESCE(OLD.initial_specification_digest,'')
          OR NEW.specification_metadata_json<>OLD.specification_metadata_json
        BEGIN SELECT RAISE(ABORT, 'autonomous mission identity is immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_single_axis_update
        BEFORE UPDATE ON autonomous_missions
        WHEN NEW.phase<>OLD.phase AND NEW.disposition<>OLD.disposition
        BEGIN SELECT RAISE(ABORT, 'mission phase and disposition must change separately'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_phase_transition
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN NEW.phase<>OLD.phase AND NOT (
            (OLD.phase='DRAFT' AND NEW.phase='SPECIFICATION_ANALYSIS') OR
            (OLD.phase='SPECIFICATION_ANALYSIS' AND NEW.phase='BACKLOG_GENERATION') OR
            (OLD.phase='BACKLOG_GENERATION' AND NEW.phase IN
                ('SPECIFICATION_ANALYSIS','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='WAITING_FOR_BACKLOG_APPROVAL' AND NEW.phase IN
                ('BACKLOG_GENERATION','APPROVED')) OR
            (OLD.phase='APPROVED' AND NEW.phase='ENVIRONMENT_DISCOVERY') OR
            (OLD.phase='ENVIRONMENT_DISCOVERY' AND NEW.phase='ENVIRONMENT_BOOTSTRAP') OR
            (OLD.phase='ENVIRONMENT_BOOTSTRAP' AND NEW.phase='DEVELOPMENT') OR
            (OLD.phase='DEVELOPMENT' AND NEW.phase IN ('VALIDATION','FINAL_VALIDATION')) OR
            (OLD.phase='VALIDATION' AND NEW.phase IN ('DEVELOPMENT','INTEGRATION')) OR
            (OLD.phase='INTEGRATION' AND NEW.phase IN ('DEVELOPMENT','FINAL_VALIDATION')) OR
            (OLD.phase='FINAL_VALIDATION' AND NEW.phase IN ('DEVELOPMENT','COMPLETED'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid autonomous mission phase transition'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_phase_requires_running
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN NEW.phase<>OLD.phase AND OLD.disposition<>'RUNNING'
        BEGIN SELECT RAISE(ABORT, 'mission phase cannot advance while execution is fenced'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_disposition_transition
        BEFORE UPDATE OF disposition ON autonomous_missions
        WHEN NEW.disposition<>OLD.disposition AND NOT (
            (OLD.disposition='RUNNING' AND NEW.disposition IN
                ('PAUSED','STOPPED','NEEDS_ATTENTION','NEEDS_HUMAN_ACTION',
                 'REPLANNING','RECOVERING','FAILED')) OR
            (OLD.disposition='PAUSED' AND NEW.disposition IN
                ('RUNNING','STOPPED','FAILED')) OR
            (OLD.disposition='STOPPED' AND NEW.disposition IN
                ('RUNNING','RECOVERING','FAILED')) OR
            (OLD.disposition='NEEDS_ATTENTION' AND NEW.disposition IN
                ('RUNNING','STOPPED','REPLANNING','FAILED')) OR
            (OLD.disposition='NEEDS_HUMAN_ACTION' AND NEW.disposition IN
                ('RUNNING','STOPPED','RECOVERING','FAILED')) OR
            (OLD.disposition='REPLANNING' AND NEW.disposition IN
                ('RUNNING','PAUSED','STOPPED','NEEDS_ATTENTION','FAILED')) OR
            (OLD.disposition='RECOVERING' AND NEW.disposition IN
                ('RUNNING','STOPPED','NEEDS_ATTENTION','NEEDS_HUMAN_ACTION','FAILED')) OR
            (OLD.disposition='FAILED' AND NEW.disposition IN ('RECOVERING','STOPPED'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid autonomous mission disposition transition'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_completed_immutable
        BEFORE UPDATE ON autonomous_missions
        WHEN OLD.phase='COMPLETED'
        BEGIN SELECT RAISE(ABORT, 'completed autonomous missions are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_version_step
        BEFORE UPDATE ON autonomous_missions
        WHEN NEW.version<>OLD.version+1
        BEGIN SELECT RAISE(ABORT, 'autonomous mission version must advance once'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_missions_state_evidence_required
        BEFORE UPDATE ON autonomous_missions
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_mission_state_versions s
             WHERE s.mission_id=OLD.id AND s.version=NEW.version
               AND s.phase=NEW.phase AND s.disposition=NEW.disposition
               AND s.configuration_digest=NEW.configuration_digest
               AND COALESCE(s.active_backlog_revision_id,-1)=
                   COALESCE(NEW.active_backlog_revision_id,-1)
               AND COALESCE(s.active_execution_epoch_id,-1)=
                   COALESCE(NEW.active_execution_epoch_id,-1)
               AND COALESCE(s.current_checkpoint_id,-1)=
                   COALESCE(NEW.current_checkpoint_id,-1)
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous mission state evidence is required'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_mission_versions_no_update
        BEFORE UPDATE ON autonomous_mission_state_versions
        BEGIN SELECT RAISE(ABORT, 'mission state versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_mission_versions_no_delete
        BEFORE DELETE ON autonomous_mission_state_versions
        BEGIN SELECT RAISE(ABORT, 'mission state versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_mission_commands_no_update
        BEFORE UPDATE ON autonomous_mission_commands
        BEGIN SELECT RAISE(ABORT, 'mission commands are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_mission_commands_no_delete
        BEFORE DELETE ON autonomous_mission_commands
        BEGIN SELECT RAISE(ABORT, 'mission commands are immutable'); END;
    """),
    (59, """
        CREATE TABLE IF NOT EXISTS autonomous_backlog_revisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            revision_number INTEGER NOT NULL CHECK(revision_number > 0),
            parent_revision_id INTEGER REFERENCES autonomous_backlog_revisions(id),
            origin TEXT NOT NULL CHECK(origin IN
                ('HUMAN','AGENT_MATERIAL','TECHNICAL_SUBTASK')),
            created_by TEXT NOT NULL,
            rationale TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version IN (1,2)),
            source_sha256 TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            revision_digest TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK(item_count > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,revision_number),
            UNIQUE(mission_id,revision_digest)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_revision_parent
            ON autonomous_backlog_revisions(mission_id,parent_revision_id);

        CREATE TABLE IF NOT EXISTS autonomous_backlog_items(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            revision_id INTEGER NOT NULL REFERENCES autonomous_backlog_revisions(id),
            stable_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN
                ('epic','feature','story','task','bug','research','change')),
            executable INTEGER NOT NULL CHECK(executable IN (0,1)),
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            parent_stable_id TEXT,
            dependencies_json TEXT NOT NULL,
            priority TEXT NOT NULL,
            acceptance_criteria_json TEXT NOT NULL,
            validation_method_json TEXT NOT NULL,
            required_components_json TEXT NOT NULL,
            required_infrastructure_json TEXT NOT NULL,
            expected_artifacts_json TEXT NOT NULL,
            definition_of_done_json TEXT NOT NULL,
            assigned_role TEXT NOT NULL,
            source_references_json TEXT NOT NULL,
            review_notes_json TEXT NOT NULL,
            labels_json TEXT NOT NULL,
            item_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(revision_id,stable_id)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_items_ready
            ON autonomous_backlog_items(revision_id,executable,stable_id);

        CREATE TABLE IF NOT EXISTS autonomous_backlog_item_states(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            item_id INTEGER NOT NULL REFERENCES autonomous_backlog_items(id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            status TEXT NOT NULL CHECK(status IN
                ('DONE','RUNNING','READY','BLOCKED','FAILED','STALE','PROPOSED')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            validation_result_json TEXT NOT NULL DEFAULT '{}',
            git_commit_sha TEXT,
            checkpoint_id INTEGER,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            carried_from_state_id INTEGER REFERENCES autonomous_backlog_item_states(id),
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_id,sequence),
            UNIQUE(item_id,command_id)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_item_state
            ON autonomous_backlog_item_states(item_id,sequence DESC);

        CREATE TABLE IF NOT EXISTS autonomous_backlog_impacts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            revision_id INTEGER NOT NULL REFERENCES autonomous_backlog_revisions(id),
            prior_revision_id INTEGER REFERENCES autonomous_backlog_revisions(id),
            stable_id TEXT NOT NULL,
            classification TEXT NOT NULL CHECK(classification IN
                ('VALID','STALE','PARTIALLY_AFFECTED','REMOVED','NEW')),
            changed_fields_json TEXT NOT NULL,
            prior_item_digest TEXT,
            current_item_digest TEXT,
            rationale TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(revision_id,stable_id)
        );

        CREATE TABLE IF NOT EXISTS autonomous_backlog_commands(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            command_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_commands_mission
            ON autonomous_backlog_commands(mission_id,id);

        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_revisions_no_update
        BEFORE UPDATE ON autonomous_backlog_revisions
        BEGIN SELECT RAISE(ABORT, 'backlog revisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_revisions_no_delete
        BEFORE DELETE ON autonomous_backlog_revisions
        BEGIN SELECT RAISE(ABORT, 'backlog revisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_items_no_update
        BEFORE UPDATE ON autonomous_backlog_items
        BEGIN SELECT RAISE(ABORT, 'backlog revision items are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_items_no_delete
        BEFORE DELETE ON autonomous_backlog_items
        BEGIN SELECT RAISE(ABORT, 'backlog revision items are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_states_no_update
        BEFORE UPDATE ON autonomous_backlog_item_states
        BEGIN SELECT RAISE(ABORT, 'backlog item state evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_states_no_delete
        BEFORE DELETE ON autonomous_backlog_item_states
        BEGIN SELECT RAISE(ABORT, 'backlog item state evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_impacts_no_update
        BEFORE UPDATE ON autonomous_backlog_impacts
        BEGIN SELECT RAISE(ABORT, 'backlog impacts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_impacts_no_delete
        BEFORE DELETE ON autonomous_backlog_impacts
        BEGIN SELECT RAISE(ABORT, 'backlog impacts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_commands_no_update
        BEFORE UPDATE ON autonomous_backlog_commands
        BEGIN SELECT RAISE(ABORT, 'backlog commands are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_commands_no_delete
        BEFORE DELETE ON autonomous_backlog_commands
        BEGIN SELECT RAISE(ABORT, 'backlog commands are immutable'); END;
    """),
    (60, """
        CREATE TABLE IF NOT EXISTS autonomous_mission_execution_epochs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            epoch_number INTEGER NOT NULL CHECK(epoch_number > 0),
            base_backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            base_backlog_revision_digest TEXT NOT NULL
                CHECK(length(base_backlog_revision_digest)=64
                      AND base_backlog_revision_digest NOT GLOB '*[^0-9a-f]*'),
            base_checkpoint_id INTEGER
                REFERENCES autonomous_mission_checkpoints(id)
                DEFERRABLE INITIALLY DEFERRED,
            base_checkpoint_digest TEXT
                CHECK(base_checkpoint_digest IS NULL OR
                      (length(base_checkpoint_digest)=64
                       AND base_checkpoint_digest NOT GLOB '*[^0-9a-f]*')),
            base_git_commit_sha TEXT NOT NULL
                CHECK(length(base_git_commit_sha) IN (40,64)
                      AND base_git_commit_sha NOT GLOB '*[^0-9a-f]*'),
            epoch_branch TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN
                ('INITIAL','CHECKPOINT_RESTART','BACKLOG_REVISION_RESTART','RECOVERY')),
            temporal_workflow_id TEXT NOT NULL,
            temporal_first_run_id TEXT NOT NULL,
            temporal_chain_metadata_json TEXT NOT NULL,
            temporal_chain_metadata_digest TEXT NOT NULL
                CHECK(length(temporal_chain_metadata_digest)=64
                      AND temporal_chain_metadata_digest NOT GLOB '*[^0-9a-f]*'),
            supersedes_epoch_id INTEGER
                REFERENCES autonomous_mission_execution_epochs(id),
            activation_mission_version INTEGER NOT NULL
                CHECK(activation_mission_version > 1),
            created_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(mission_id,epoch_number),
            UNIQUE(mission_id,epoch_branch),
            UNIQUE(mission_id,temporal_workflow_id),
            CHECK((base_checkpoint_id IS NULL) =
                  (base_checkpoint_digest IS NULL)),
            CHECK((epoch_number=1 AND base_checkpoint_id IS NULL
                   AND supersedes_epoch_id IS NULL AND origin='INITIAL') OR
                  (epoch_number>1 AND base_checkpoint_id IS NOT NULL
                   AND supersedes_epoch_id IS NOT NULL AND origin<>'INITIAL'))
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_epochs_mission
            ON autonomous_mission_execution_epochs(mission_id,epoch_number);

        CREATE TABLE IF NOT EXISTS autonomous_mission_checkpoints(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            checkpoint_key TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            checkpoint_type TEXT NOT NULL CHECK(checkpoint_type IN
                ('BACKLOG_APPROVED','ENVIRONMENT_BOOTSTRAPPED',
                 'ARCHITECTURE_BASELINE','WORK_ITEM_ACCEPTED','REPAIR_ACCEPTED',
                 'BACKLOG_REVISION_APPLIED','INTEGRATION_MILESTONE',
                 'FINAL_VALIDATION','MANUAL')),
            reason TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            backlog_revision_digest TEXT NOT NULL
                CHECK(length(backlog_revision_digest)=64
                      AND backlog_revision_digest NOT GLOB '*[^0-9a-f]*'),
            current_work_item_stable_id TEXT,
            completed_work_items_json TEXT NOT NULL,
            pending_work_items_json TEXT NOT NULL,
            git_commit_sha TEXT NOT NULL
                CHECK(length(git_commit_sha) IN (40,64)
                      AND git_commit_sha NOT GLOB '*[^0-9a-f]*'),
            git_branch TEXT NOT NULL,
            git_worktree_path TEXT NOT NULL,
            architecture_version TEXT,
            architecture_digest TEXT
                CHECK(architecture_digest IS NULL OR
                      (length(architecture_digest)=64
                       AND architecture_digest NOT GLOB '*[^0-9a-f]*')),
            environment_manifest_version TEXT,
            environment_manifest_digest TEXT
                CHECK(environment_manifest_digest IS NULL OR
                      (length(environment_manifest_digest)=64
                       AND environment_manifest_digest NOT GLOB '*[^0-9a-f]*')),
            role_model_assignments_json TEXT NOT NULL,
            role_model_assignments_digest TEXT NOT NULL
                CHECK(length(role_model_assignments_digest)=64
                      AND role_model_assignments_digest NOT GLOB '*[^0-9a-f]*'),
            artifacts_json TEXT NOT NULL,
            memory_context_json TEXT NOT NULL,
            service_manifest_version TEXT,
            service_manifest_digest TEXT
                CHECK(service_manifest_digest IS NULL OR
                      (length(service_manifest_digest)=64
                       AND service_manifest_digest NOT GLOB '*[^0-9a-f]*')),
            validation_state_json TEXT NOT NULL,
            validation_state_digest TEXT NOT NULL
                CHECK(length(validation_state_digest)=64
                      AND validation_state_digest NOT GLOB '*[^0-9a-f]*'),
            document_json TEXT NOT NULL,
            checkpoint_digest TEXT NOT NULL
                CHECK(length(checkpoint_digest)=64
                      AND checkpoint_digest NOT GLOB '*[^0-9a-f]*'),
            UNIQUE(execution_epoch_id,sequence),
            UNIQUE(mission_id,checkpoint_digest),
            CHECK((architecture_version IS NULL) =
                  (architecture_digest IS NULL)),
            CHECK((environment_manifest_version IS NULL) =
                  (environment_manifest_digest IS NULL)),
            CHECK((service_manifest_version IS NULL) =
                  (service_manifest_digest IS NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_checkpoints_mission
            ON autonomous_mission_checkpoints(mission_id,created_at,id);
        CREATE INDEX IF NOT EXISTS idx_autonomous_checkpoints_epoch
            ON autonomous_mission_checkpoints(execution_epoch_id,sequence);

        CREATE TABLE IF NOT EXISTS autonomous_epoch_supersessions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            superseded_epoch_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_execution_epochs(id),
            superseding_epoch_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_execution_epochs(id),
            selected_checkpoint_id INTEGER NOT NULL
                REFERENCES autonomous_mission_checkpoints(id),
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(superseded_epoch_id<>superseding_epoch_id)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_epoch_supersessions_mission
            ON autonomous_epoch_supersessions(mission_id,id);

        CREATE TABLE IF NOT EXISTS autonomous_epoch_temporal_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            sequence INTEGER NOT NULL CHECK(sequence > 0),
            workflow_id TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            previous_run_id TEXT,
            workflow_build_id TEXT,
            metadata_json TEXT NOT NULL,
            metadata_digest TEXT NOT NULL
                CHECK(length(metadata_digest)=64
                      AND metadata_digest NOT GLOB '*[^0-9a-f]*'),
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            UNIQUE(execution_epoch_id,sequence),
            UNIQUE(execution_epoch_id,workflow_id,run_id),
            CHECK((sequence=1 AND previous_run_id IS NULL) OR
                  (sequence>1 AND previous_run_id IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_epoch_temporal_chain
            ON autonomous_epoch_temporal_runs(execution_epoch_id,sequence);

        ALTER TABLE autonomous_backlog_item_states
            ADD COLUMN execution_epoch_id INTEGER
                REFERENCES autonomous_mission_execution_epochs(id);
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_state_epoch
            ON autonomous_backlog_item_states(execution_epoch_id,item_id,sequence);

        CREATE TRIGGER IF NOT EXISTS autonomous_epochs_no_update
        BEFORE UPDATE ON autonomous_mission_execution_epochs
        BEGIN SELECT RAISE(ABORT, 'mission execution epochs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_epochs_no_delete
        BEFORE DELETE ON autonomous_mission_execution_epochs
        BEGIN SELECT RAISE(ABORT, 'mission execution epochs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_checkpoints_no_update
        BEFORE UPDATE ON autonomous_mission_checkpoints
        BEGIN SELECT RAISE(ABORT, 'mission checkpoints are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_checkpoints_no_delete
        BEFORE DELETE ON autonomous_mission_checkpoints
        BEGIN SELECT RAISE(ABORT, 'mission checkpoints are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_supersessions_no_update
        BEFORE UPDATE ON autonomous_epoch_supersessions
        BEGIN SELECT RAISE(ABORT, 'epoch supersessions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_supersessions_no_delete
        BEFORE DELETE ON autonomous_epoch_supersessions
        BEGIN SELECT RAISE(ABORT, 'epoch supersessions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_temporal_runs_no_update
        BEFORE UPDATE ON autonomous_epoch_temporal_runs
        BEGIN SELECT RAISE(ABORT, 'epoch Temporal runs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_temporal_runs_no_delete
        BEFORE DELETE ON autonomous_epoch_temporal_runs
        BEGIN SELECT RAISE(ABORT, 'epoch Temporal runs are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_sequence_valid
        BEFORE INSERT ON autonomous_mission_execution_epochs
        WHEN NEW.epoch_number<>(
            SELECT COALESCE(MAX(epoch_number),0)+1
              FROM autonomous_mission_execution_epochs
             WHERE mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission epoch sequence must be append-only'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_revision_valid
        BEFORE INSERT ON autonomous_mission_execution_epochs
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_backlog_revisions r
             WHERE r.id=NEW.base_backlog_revision_id
               AND r.mission_id=NEW.mission_id
               AND r.revision_digest=NEW.base_backlog_revision_digest
        )
        BEGIN SELECT RAISE(ABORT, 'mission epoch backlog revision is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_predecessor_valid
        BEFORE INSERT ON autonomous_mission_execution_epochs
        WHEN NEW.supersedes_epoch_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_execution_epochs e
             WHERE e.id=NEW.supersedes_epoch_id
               AND e.mission_id=NEW.mission_id
               AND e.epoch_number=NEW.epoch_number-1
        )
        BEGIN SELECT RAISE(ABORT, 'mission epoch predecessor is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_epoch_checkpoint_valid
        BEFORE INSERT ON autonomous_mission_execution_epochs
        WHEN NEW.base_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_checkpoints c
             WHERE c.id=NEW.base_checkpoint_id
               AND c.mission_id=NEW.mission_id
               AND c.checkpoint_digest=NEW.base_checkpoint_digest
               AND c.git_commit_sha=NEW.base_git_commit_sha
        )
        BEGIN SELECT RAISE(ABORT, 'mission epoch base checkpoint is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_checkpoint_sequence_valid
        BEFORE INSERT ON autonomous_mission_checkpoints
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_mission_checkpoints
             WHERE execution_epoch_id=NEW.execution_epoch_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission checkpoint sequence must be append-only'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_checkpoint_epoch_valid
        BEFORE INSERT ON autonomous_mission_checkpoints
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_mission_execution_epochs e
             WHERE e.id=NEW.execution_epoch_id AND e.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission checkpoint epoch is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_checkpoint_revision_valid
        BEFORE INSERT ON autonomous_mission_checkpoints
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_backlog_revisions r
             WHERE r.id=NEW.backlog_revision_id
               AND r.mission_id=NEW.mission_id
               AND r.revision_digest=NEW.backlog_revision_digest
        )
        BEGIN SELECT RAISE(ABORT, 'mission checkpoint backlog revision is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_supersession_scope_valid
        BEFORE INSERT ON autonomous_epoch_supersessions
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_execution_epochs old
              JOIN autonomous_mission_execution_epochs new
                ON new.id=NEW.superseding_epoch_id
             WHERE old.id=NEW.superseded_epoch_id
               AND old.mission_id=NEW.mission_id
               AND new.mission_id=NEW.mission_id
               AND new.supersedes_epoch_id=old.id
        ) OR NOT EXISTS (
            SELECT 1 FROM autonomous_mission_checkpoints c
             WHERE c.id=NEW.selected_checkpoint_id
               AND c.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission epoch supersession scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_temporal_run_sequence_valid
        BEFORE INSERT ON autonomous_epoch_temporal_runs
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_epoch_temporal_runs
             WHERE execution_epoch_id=NEW.execution_epoch_id
        )
        BEGIN SELECT RAISE(ABORT, 'epoch Temporal run sequence must be append-only'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_temporal_run_chain_valid
        BEFORE INSERT ON autonomous_epoch_temporal_runs
        WHEN NEW.sequence>1 AND NOT EXISTS (
            SELECT 1 FROM autonomous_epoch_temporal_runs r
             WHERE r.execution_epoch_id=NEW.execution_epoch_id
               AND r.sequence=NEW.sequence-1
               AND r.run_id=NEW.previous_run_id
               AND r.workflow_id=NEW.workflow_id
        )
        BEGIN SELECT RAISE(ABORT, 'epoch Temporal run chain is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_mission_active_epoch_valid
        BEFORE UPDATE OF active_execution_epoch_id ON autonomous_missions
        WHEN NEW.active_execution_epoch_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_execution_epochs e
             WHERE e.id=NEW.active_execution_epoch_id AND e.mission_id=NEW.id
        )
        BEGIN SELECT RAISE(ABORT, 'active mission epoch is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_mission_current_checkpoint_valid
        BEFORE UPDATE OF current_checkpoint_id ON autonomous_missions
        WHEN NEW.current_checkpoint_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_checkpoints c
             WHERE c.id=NEW.current_checkpoint_id AND c.mission_id=NEW.id
        )
        BEGIN SELECT RAISE(ABORT, 'current mission checkpoint is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_state_epoch_valid
        BEFORE INSERT ON autonomous_backlog_item_states
        WHEN NEW.execution_epoch_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_execution_epochs e
              JOIN autonomous_backlog_revisions r ON r.mission_id=e.mission_id
              JOIN autonomous_backlog_items i ON i.revision_id=r.id
             WHERE e.id=NEW.execution_epoch_id AND i.id=NEW.item_id
        )
        BEGIN SELECT RAISE(ABORT, 'backlog item execution epoch is invalid'); END;
    """),
    (61, """
        CREATE TABLE IF NOT EXISTS autonomous_local_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            backlog_revision_digest TEXT NOT NULL
                CHECK(length(backlog_revision_digest)=64
                      AND backlog_revision_digest NOT GLOB '*[^0-9a-f]*'),
            execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            epoch_branch TEXT NOT NULL,
            repository_path TEXT NOT NULL,
            provider_ids_json TEXT NOT NULL,
            role_model_manifest_json TEXT NOT NULL,
            role_model_manifest_digest TEXT NOT NULL
                CHECK(length(role_model_manifest_digest)=64
                      AND role_model_manifest_digest NOT GLOB '*[^0-9a-f]*'),
            allowed_permissions_json TEXT NOT NULL,
            tool_profile TEXT NOT NULL,
            bootstrap_profile TEXT NOT NULL,
            policy_version INTEGER NOT NULL CHECK(policy_version > 0),
            policy_snapshot_json TEXT NOT NULL,
            policy_digest TEXT NOT NULL
                CHECK(length(policy_digest)=64
                      AND policy_digest NOT GLOB '*[^0-9a-f]*'),
            granted_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            authorization_digest TEXT NOT NULL UNIQUE
                CHECK(length(authorization_digest)=64
                      AND authorization_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            UNIQUE(mission_id,backlog_revision_id,execution_epoch_id,policy_digest)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_local_authorizations
            ON autonomous_local_authorizations(mission_id,created_at,id);

        CREATE TABLE IF NOT EXISTS autonomous_authorization_revocations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            authorization_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_local_authorizations(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_planning_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            planning_request_id TEXT NOT NULL,
            requested_action TEXT NOT NULL CHECK(requested_action IN
                ('ANALYZE','REGENERATE_BACKLOG')),
            provider_ids_json TEXT NOT NULL,
            role_models_json TEXT NOT NULL,
            repository_path TEXT NOT NULL,
            allowed_permissions_json TEXT NOT NULL,
            tool_profile TEXT NOT NULL,
            policy_version INTEGER NOT NULL CHECK(policy_version > 0),
            policy_snapshot_json TEXT NOT NULL,
            policy_digest TEXT NOT NULL
                CHECK(length(policy_digest)=64
                      AND policy_digest NOT GLOB '*[^0-9a-f]*'),
            authorized_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            authorization_digest TEXT NOT NULL UNIQUE
                CHECK(length(authorization_digest)=64
                      AND authorization_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(mission_id,planning_request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_authorizations
            ON autonomous_planning_authorizations(mission_id,expires_at,id);

        CREATE TABLE IF NOT EXISTS autonomous_planning_authorization_closures(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            planning_authorization_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_authorizations(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_authorization_decisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL,
            request_json TEXT NOT NULL,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            outcome TEXT NOT NULL CHECK(outcome IN
                ('ALLOW_AUTONOMOUS','ALLOW_PLANNING',
                 'REQUIRE_STANDARD_GATE','DENY')),
            reason TEXT NOT NULL,
            authority_valid INTEGER NOT NULL CHECK(authority_valid IN (0,1)),
            autonomous_authorization_id INTEGER
                REFERENCES autonomous_local_authorizations(id),
            planning_authorization_id INTEGER
                REFERENCES autonomous_planning_authorizations(id),
            policy_version INTEGER NOT NULL CHECK(policy_version > 0),
            policy_digest TEXT NOT NULL
                CHECK(length(policy_digest)=64
                      AND policy_digest NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL,
            decision_digest TEXT NOT NULL UNIQUE
                CHECK(length(decision_digest)=64
                      AND decision_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_authorization_decisions
            ON autonomous_authorization_decisions(mission_id,created_at,id);

        CREATE TABLE IF NOT EXISTS autonomous_authorization_commands(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            command_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_authorization_commands
            ON autonomous_authorization_commands(mission_id,id);

        CREATE TRIGGER IF NOT EXISTS autonomous_local_authorizations_no_update
        BEFORE UPDATE ON autonomous_local_authorizations
        BEGIN SELECT RAISE(ABORT, 'autonomous authorizations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_local_authorizations_no_delete
        BEFORE DELETE ON autonomous_local_authorizations
        BEGIN SELECT RAISE(ABORT, 'autonomous authorizations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_revocations_no_update
        BEFORE UPDATE ON autonomous_authorization_revocations
        BEGIN SELECT RAISE(ABORT, 'authorization revocations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_revocations_no_delete
        BEFORE DELETE ON autonomous_authorization_revocations
        BEGIN SELECT RAISE(ABORT, 'authorization revocations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_authorizations_no_update
        BEFORE UPDATE ON autonomous_planning_authorizations
        BEGIN SELECT RAISE(ABORT, 'planning authorizations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_authorizations_no_delete
        BEFORE DELETE ON autonomous_planning_authorizations
        BEGIN SELECT RAISE(ABORT, 'planning authorizations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_closures_no_update
        BEFORE UPDATE ON autonomous_planning_authorization_closures
        BEGIN SELECT RAISE(ABORT, 'planning authorization closures are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_closures_no_delete
        BEFORE DELETE ON autonomous_planning_authorization_closures
        BEGIN SELECT RAISE(ABORT, 'planning authorization closures are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_decisions_no_update
        BEFORE UPDATE ON autonomous_authorization_decisions
        BEGIN SELECT RAISE(ABORT, 'authorization decisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_decisions_no_delete
        BEFORE DELETE ON autonomous_authorization_decisions
        BEGIN SELECT RAISE(ABORT, 'authorization decisions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_commands_no_update
        BEFORE UPDATE ON autonomous_authorization_commands
        BEGIN SELECT RAISE(ABORT, 'authorization commands are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_authorization_commands_no_delete
        BEFORE DELETE ON autonomous_authorization_commands
        BEGIN SELECT RAISE(ABORT, 'authorization commands are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_local_authorization_scope_valid
        BEFORE INSERT ON autonomous_local_authorizations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions m
              JOIN autonomous_backlog_revisions r
                ON r.id=NEW.backlog_revision_id AND r.mission_id=m.id
              JOIN autonomous_mission_execution_epochs e
                ON e.id=NEW.execution_epoch_id AND e.mission_id=m.id
             WHERE m.id=NEW.mission_id
               AND m.active_backlog_revision_id=NEW.backlog_revision_id
               AND m.active_execution_epoch_id=NEW.execution_epoch_id
               AND r.revision_digest=NEW.backlog_revision_digest
               AND e.epoch_branch=NEW.epoch_branch
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous authorization scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_revocation_scope_valid
        BEFORE INSERT ON autonomous_authorization_revocations
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_local_authorizations a
             WHERE a.id=NEW.authorization_id AND a.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'authorization revocation scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_scope_valid
        BEFORE INSERT ON autonomous_planning_authorizations
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_missions m WHERE m.id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'planning authorization mission is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_closure_scope_valid
        BEFORE INSERT ON autonomous_planning_authorization_closures
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_planning_authorizations p
             WHERE p.id=NEW.planning_authorization_id
               AND p.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'planning authorization closure scope is invalid'); END;
    """),
    (62, """
        CREATE TABLE IF NOT EXISTS autonomous_mission_specification_sources(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            version INTEGER NOT NULL CHECK(version > 0),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('TEXT','UPLOAD')),
            source_name TEXT NOT NULL,
            media_type TEXT NOT NULL CHECK(media_type IN (
                'text/plain','text/markdown','application/json','application/pdf'
            )),
            provenance TEXT NOT NULL,
            actor TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_digest TEXT NOT NULL
                CHECK(length(content_digest)=64
                      AND content_digest NOT GLOB '*[^0-9a-f]*'),
            raw_digest TEXT NOT NULL
                CHECK(length(raw_digest)=64
                      AND raw_digest NOT GLOB '*[^0-9a-f]*'),
            byte_count INTEGER NOT NULL CHECK(byte_count > 0),
            metadata_json TEXT NOT NULL,
            source_digest TEXT NOT NULL UNIQUE
                CHECK(length(source_digest)=64
                      AND source_digest NOT GLOB '*[^0-9a-f]*'),
            intake_source_id INTEGER REFERENCES mission_sources(id),
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            UNIQUE(mission_id,version)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_specification_sources
            ON autonomous_mission_specification_sources(mission_id,version);

        CREATE TABLE IF NOT EXISTS autonomous_mission_specification_heads(
            mission_id INTEGER PRIMARY KEY REFERENCES autonomous_missions(id),
            source_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_specification_sources(id),
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            source_digest TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_specification_supersessions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            previous_source_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_specification_sources(id),
            replacement_source_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_specification_sources(id),
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(previous_source_id<>replacement_source_id)
        );

        CREATE TABLE IF NOT EXISTS autonomous_backlog_revision_invalidations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            revision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_revisions(id),
            previous_source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            replacement_source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            revision_source_digest TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_revision_invalidations
            ON autonomous_backlog_revision_invalidations(mission_id,revision_id);

        CREATE TABLE IF NOT EXISTS autonomous_specification_commands(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            command_type TEXT NOT NULL CHECK(command_type IN
                ('CREATE_SOURCE','UPDATE_SOURCE')),
            actor TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            result_source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_planning_manifests(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            specification_source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            specification_source_digest TEXT NOT NULL
                CHECK(length(specification_source_digest)=64
                      AND specification_source_digest NOT GLOB '*[^0-9a-f]*'),
            proposal_key TEXT NOT NULL,
            role_pack_id INTEGER NOT NULL REFERENCES software_role_packs(id),
            default_provider_id TEXT NOT NULL,
            default_model TEXT NOT NULL,
            assignments_json TEXT NOT NULL,
            context_policy_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE
                CHECK(length(manifest_digest)=64
                      AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
            created_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(mission_id,proposal_key)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_manifests
            ON autonomous_planning_manifests(mission_id,specification_source_id,id);

        CREATE TABLE IF NOT EXISTS autonomous_planning_manifest_revision_bindings(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            manifest_id INTEGER NOT NULL REFERENCES autonomous_planning_manifests(id),
            manifest_digest TEXT NOT NULL
                CHECK(length(manifest_digest)=64
                      AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
            revision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_revisions(id),
            revision_digest TEXT NOT NULL,
            actor TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(manifest_id,revision_id)
        );

        CREATE TABLE IF NOT EXISTS autonomous_planning_contexts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            manifest_id INTEGER NOT NULL REFERENCES autonomous_planning_manifests(id),
            role_id TEXT NOT NULL,
            invocation_sequence INTEGER NOT NULL CHECK(invocation_sequence > 0),
            context_key TEXT NOT NULL UNIQUE,
            context_json TEXT NOT NULL,
            context_digest TEXT NOT NULL UNIQUE
                CHECK(length(context_digest)=64
                      AND context_digest NOT GLOB '*[^0-9a-f]*'),
            byte_count INTEGER NOT NULL CHECK(byte_count > 0),
            token_count INTEGER NOT NULL CHECK(token_count > 0),
            read_only INTEGER NOT NULL CHECK(read_only=1),
            fresh_session INTEGER NOT NULL CHECK(fresh_session=1),
            created_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(manifest_id,role_id,invocation_sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_contexts
            ON autonomous_planning_contexts(manifest_id,role_id,invocation_sequence);

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_sources_no_update
        BEFORE UPDATE ON autonomous_mission_specification_sources
        BEGIN SELECT RAISE(ABORT, 'mission specification sources are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_sources_no_delete
        BEFORE DELETE ON autonomous_mission_specification_sources
        BEGIN SELECT RAISE(ABORT, 'mission specification sources are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_supersessions_no_update
        BEFORE UPDATE ON autonomous_specification_supersessions
        BEGIN SELECT RAISE(ABORT, 'specification supersessions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_supersessions_no_delete
        BEFORE DELETE ON autonomous_specification_supersessions
        BEGIN SELECT RAISE(ABORT, 'specification supersessions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_revision_invalidations_no_update
        BEFORE UPDATE ON autonomous_backlog_revision_invalidations
        BEGIN SELECT RAISE(ABORT, 'backlog revision invalidations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_revision_invalidations_no_delete
        BEFORE DELETE ON autonomous_backlog_revision_invalidations
        BEGIN SELECT RAISE(ABORT, 'backlog revision invalidations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_commands_no_update
        BEFORE UPDATE ON autonomous_specification_commands
        BEGIN SELECT RAISE(ABORT, 'specification commands are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_commands_no_delete
        BEFORE DELETE ON autonomous_specification_commands
        BEGIN SELECT RAISE(ABORT, 'specification commands are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_manifests_no_update
        BEFORE UPDATE ON autonomous_planning_manifests
        BEGIN SELECT RAISE(ABORT, 'planning manifests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_manifests_no_delete
        BEFORE DELETE ON autonomous_planning_manifests
        BEGIN SELECT RAISE(ABORT, 'planning manifests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_bindings_no_update
        BEFORE UPDATE ON autonomous_planning_manifest_revision_bindings
        BEGIN SELECT RAISE(ABORT, 'planning manifest bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_bindings_no_delete
        BEFORE DELETE ON autonomous_planning_manifest_revision_bindings
        BEGIN SELECT RAISE(ABORT, 'planning manifest bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_contexts_no_update
        BEFORE UPDATE ON autonomous_planning_contexts
        BEGIN SELECT RAISE(ABORT, 'planning contexts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_contexts_no_delete
        BEFORE DELETE ON autonomous_planning_contexts
        BEGIN SELECT RAISE(ABORT, 'planning contexts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_specification_heads_no_delete
        BEFORE DELETE ON autonomous_mission_specification_heads
        BEGIN SELECT RAISE(ABORT, 'specification head projection is durable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_source_phase_valid
        BEFORE INSERT ON autonomous_mission_specification_sources
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_missions m
             WHERE m.id=NEW.mission_id AND m.phase IN (
                'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION',
                'WAITING_FOR_BACKLOG_APPROVAL'
             )
        )
        BEGIN SELECT RAISE(ABORT, 'specification changes are pre-approval only'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_source_sequence_valid
        BEFORE INSERT ON autonomous_mission_specification_sources
        WHEN NEW.version<>COALESCE((
            SELECT MAX(s.version)+1
              FROM autonomous_mission_specification_sources s
             WHERE s.mission_id=NEW.mission_id
        ),1)
        BEGIN SELECT RAISE(ABORT, 'specification source version is not sequential'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_head_scope_valid_insert
        BEFORE INSERT ON autonomous_mission_specification_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_mission_specification_sources s
             WHERE s.id=NEW.source_id AND s.mission_id=NEW.mission_id
               AND s.version=NEW.source_version
               AND s.source_digest=NEW.source_digest
        )
        BEGIN SELECT RAISE(ABORT, 'specification head scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_head_scope_valid_update
        BEFORE UPDATE ON autonomous_mission_specification_heads
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_mission_specification_sources s
             WHERE s.id=NEW.source_id AND s.mission_id=NEW.mission_id
               AND s.version=NEW.source_version
               AND s.source_digest=NEW.source_digest
        ) OR NOT EXISTS (
            SELECT 1 FROM autonomous_specification_supersessions x
             WHERE x.mission_id=NEW.mission_id
               AND x.previous_source_id=OLD.source_id
               AND x.replacement_source_id=NEW.source_id
        )
        BEGIN SELECT RAISE(ABORT, 'specification head update lacks supersession evidence'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_specification_supersession_scope_valid
        BEFORE INSERT ON autonomous_specification_supersessions
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_specification_sources prior
              JOIN autonomous_mission_specification_sources next
                ON next.id=NEW.replacement_source_id
               AND next.mission_id=prior.mission_id
               AND next.version=prior.version+1
             WHERE prior.id=NEW.previous_source_id
               AND prior.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'specification supersession scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_revision_invalidation_scope_valid
        BEFORE INSERT ON autonomous_backlog_revision_invalidations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_backlog_revisions r
              JOIN autonomous_mission_specification_sources prior
                ON prior.id=NEW.previous_source_id
               AND prior.mission_id=r.mission_id
             JOIN autonomous_mission_specification_sources next
                ON next.id=NEW.replacement_source_id
               AND next.mission_id=r.mission_id
              JOIN autonomous_specification_supersessions x
                ON x.mission_id=r.mission_id
               AND x.previous_source_id=prior.id
               AND x.replacement_source_id=next.id
             WHERE r.id=NEW.revision_id AND r.mission_id=NEW.mission_id
               AND r.source_sha256=NEW.revision_source_digest
               AND r.source_sha256<>next.raw_digest
        )
        BEGIN SELECT RAISE(ABORT, 'backlog revision invalidation scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_invalidated_revision_not_activatable
        BEFORE UPDATE OF active_backlog_revision_id ON autonomous_missions
        WHEN NEW.active_backlog_revision_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM autonomous_backlog_revision_invalidations i
             WHERE i.mission_id=NEW.id
               AND i.revision_id=NEW.active_backlog_revision_id
        )
        BEGIN SELECT RAISE(ABORT, 'invalidated backlog revision cannot be activated'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_manifest_scope_valid
        BEFORE INSERT ON autonomous_planning_manifests
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_specification_heads h
              JOIN autonomous_mission_specification_sources s ON s.id=h.source_id
              JOIN autonomous_missions m ON m.id=h.mission_id
             WHERE h.mission_id=NEW.mission_id
               AND h.source_id=NEW.specification_source_id
               AND s.source_digest=NEW.specification_source_digest
               AND m.phase IN (
                  'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION',
                  'WAITING_FOR_BACKLOG_APPROVAL'
               )
        )
        BEGIN SELECT RAISE(ABORT, 'planning manifest source is not current'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_binding_scope_valid
        BEFORE INSERT ON autonomous_planning_manifest_revision_bindings
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_manifests p
              JOIN autonomous_backlog_revisions r
                ON r.id=NEW.revision_id AND r.mission_id=p.mission_id
             WHERE p.id=NEW.manifest_id AND p.mission_id=NEW.mission_id
               AND p.manifest_digest=NEW.manifest_digest
               AND r.revision_digest=NEW.revision_digest
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_backlog_revision_invalidations i
                    WHERE i.revision_id=r.id
               )
               AND EXISTS (
                   SELECT 1 FROM autonomous_mission_specification_heads h
                    WHERE h.mission_id=p.mission_id
                      AND h.source_id=p.specification_source_id
               )
        )
        BEGIN SELECT RAISE(ABORT, 'planning manifest revision scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_context_scope_valid
        BEFORE INSERT ON autonomous_planning_contexts
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_manifests p
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=p.mission_id
               AND h.source_id=p.specification_source_id
              JOIN autonomous_missions m ON m.id=p.mission_id
             WHERE p.id=NEW.manifest_id AND p.mission_id=NEW.mission_id
               AND m.phase IN (
                   'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION',
                   'WAITING_FOR_BACKLOG_APPROVAL'
               )
               AND m.disposition='RUNNING'
        )
        BEGIN SELECT RAISE(ABORT, 'planning context manifest scope is invalid'); END;
    """),
    (63, """
        CREATE TABLE IF NOT EXISTS autonomous_planning_pipeline_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            manifest_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_manifests(id),
            planning_authorization_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_authorizations(id),
            proposal_key TEXT NOT NULL,
            requested_action TEXT NOT NULL CHECK(requested_action IN
                ('ANALYZE','REGENERATE_BACKLOG')),
            max_attempts_per_role INTEGER NOT NULL
                CHECK(max_attempts_per_role BETWEEN 1 AND 5),
            created_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_pipeline_runs
            ON autonomous_planning_pipeline_runs(mission_id,id);

        CREATE TABLE IF NOT EXISTS autonomous_planning_pipeline_invocations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL
                REFERENCES autonomous_planning_pipeline_runs(id),
            role_id TEXT NOT NULL,
            invocation_order INTEGER NOT NULL CHECK(invocation_order BETWEEN 1 AND 5),
            attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 5),
            context_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_contexts(id),
            authorization_decision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_authorization_decisions(id),
            provider_id TEXT NOT NULL,
            model TEXT NOT NULL,
            logical_agent_id TEXT NOT NULL,
            provider_ok INTEGER NOT NULL CHECK(provider_ok IN (0,1)),
            response_text TEXT NOT NULL,
            response_digest TEXT NOT NULL
                CHECK(length(response_digest)=64
                      AND response_digest NOT GLOB '*[^0-9a-f]*'),
            provider_metadata_json TEXT NOT NULL,
            output_json TEXT,
            evidence_json TEXT,
            valid INTEGER NOT NULL CHECK(valid IN (0,1)),
            validation_errors_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id,role_id,attempt_number),
            CHECK(valid=0 OR (provider_ok=1 AND output_json IS NOT NULL
                              AND evidence_json IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_invocations
            ON autonomous_planning_pipeline_invocations(run_id,invocation_order,attempt_number);

        CREATE TABLE IF NOT EXISTS autonomous_planning_pipeline_artifacts(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL
                REFERENCES autonomous_planning_pipeline_runs(id),
            manifest_id INTEGER NOT NULL
                REFERENCES autonomous_planning_manifests(id),
            invocation_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_pipeline_invocations(id),
            role_id TEXT NOT NULL,
            invocation_order INTEGER NOT NULL CHECK(invocation_order BETWEEN 1 AND 5),
            artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
                'MISSION_ANALYSIS','NORMALIZED_REQUIREMENTS',
                'ARCHITECTURE_PROPOSAL','BACKLOG_PROPOSAL','REVIEW_REPORT'
            )),
            content_json TEXT NOT NULL,
            output_digest TEXT NOT NULL
                CHECK(length(output_digest)=64
                      AND output_digest NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL
                CHECK(length(evidence_digest)=64
                      AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
            artifact_digest TEXT NOT NULL UNIQUE
                CHECK(length(artifact_digest)=64
                      AND artifact_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            UNIQUE(run_id,role_id),
            UNIQUE(run_id,invocation_order)
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_planning_artifacts
            ON autonomous_planning_pipeline_artifacts(run_id,invocation_order);

        CREATE TABLE IF NOT EXISTS autonomous_planning_pipeline_failures(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_pipeline_runs(id),
            role_id TEXT NOT NULL,
            attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 1 AND 5),
            validation_errors_json TEXT NOT NULL,
            failure_digest TEXT NOT NULL UNIQUE
                CHECK(length(failure_digest)=64
                      AND failure_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS autonomous_planning_pipeline_completions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_pipeline_runs(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            manifest_id INTEGER NOT NULL REFERENCES autonomous_planning_manifests(id),
            manifest_revision_binding_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_manifest_revision_bindings(id),
            revision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_revisions(id),
            revision_digest TEXT NOT NULL
                CHECK(length(revision_digest)=64
                      AND revision_digest NOT GLOB '*[^0-9a-f]*'),
            final_artifact_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_planning_pipeline_artifacts(id),
            completion_digest TEXT NOT NULL UNIQUE
                CHECK(length(completion_digest)=64
                      AND completion_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_pipeline_runs_no_update
        BEFORE UPDATE ON autonomous_planning_pipeline_runs
        BEGIN SELECT RAISE(ABORT, 'planning pipeline runs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_pipeline_runs_no_delete
        BEFORE DELETE ON autonomous_planning_pipeline_runs
        BEGIN SELECT RAISE(ABORT, 'planning pipeline runs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_invocations_no_update
        BEFORE UPDATE ON autonomous_planning_pipeline_invocations
        BEGIN SELECT RAISE(ABORT, 'planning pipeline invocations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_invocations_no_delete
        BEFORE DELETE ON autonomous_planning_pipeline_invocations
        BEGIN SELECT RAISE(ABORT, 'planning pipeline invocations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_artifacts_no_update
        BEFORE UPDATE ON autonomous_planning_pipeline_artifacts
        BEGIN SELECT RAISE(ABORT, 'planning pipeline artifacts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_artifacts_no_delete
        BEFORE DELETE ON autonomous_planning_pipeline_artifacts
        BEGIN SELECT RAISE(ABORT, 'planning pipeline artifacts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_failures_no_update
        BEFORE UPDATE ON autonomous_planning_pipeline_failures
        BEGIN SELECT RAISE(ABORT, 'planning pipeline failures are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_failures_no_delete
        BEFORE DELETE ON autonomous_planning_pipeline_failures
        BEGIN SELECT RAISE(ABORT, 'planning pipeline failures are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_completions_no_update
        BEFORE UPDATE ON autonomous_planning_pipeline_completions
        BEGIN SELECT RAISE(ABORT, 'planning pipeline completions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_planning_completions_no_delete
        BEFORE DELETE ON autonomous_planning_pipeline_completions
        BEGIN SELECT RAISE(ABORT, 'planning pipeline completions are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_pipeline_run_scope_valid
        BEFORE INSERT ON autonomous_planning_pipeline_runs
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_manifests p
              JOIN autonomous_planning_authorizations a
                ON a.id=NEW.planning_authorization_id
               AND a.mission_id=p.mission_id
               AND a.planning_request_id=p.proposal_key
               AND a.requested_action=NEW.requested_action
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=p.mission_id
               AND h.source_id=p.specification_source_id
             WHERE p.id=NEW.manifest_id AND p.mission_id=NEW.mission_id
               AND p.proposal_key=NEW.proposal_key
        )
        BEGIN SELECT RAISE(ABORT, 'planning pipeline run scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_invocation_scope_valid
        BEFORE INSERT ON autonomous_planning_pipeline_invocations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_pipeline_runs r
              JOIN autonomous_planning_manifests p
                ON p.id=r.manifest_id
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=r.mission_id
               AND h.source_id=p.specification_source_id
              JOIN autonomous_planning_contexts c
                ON c.id=NEW.context_id AND c.manifest_id=r.manifest_id
              JOIN autonomous_authorization_decisions d
                ON d.id=NEW.authorization_decision_id
               AND d.mission_id=r.mission_id
               AND d.planning_authorization_id=r.planning_authorization_id
               AND d.outcome='ALLOW_PLANNING'
             WHERE r.id=NEW.run_id
               AND c.role_id=NEW.role_id
               AND c.invocation_sequence=NEW.attempt_number
               AND json_extract(
                       p.assignments_json,
                       '$[' || (NEW.invocation_order - 1) || '].role_id'
                   )=NEW.role_id
               AND json_extract(
                       p.assignments_json,
                       '$[' || (NEW.invocation_order - 1) || '].provider_id'
                   )=NEW.provider_id
               AND json_extract(
                       p.assignments_json,
                       '$[' || (NEW.invocation_order - 1) || '].model'
                   )=NEW.model
               AND json_extract(
                       p.assignments_json,
                       '$[' || (NEW.invocation_order - 1) || '].logical_agent_id'
                   )=NEW.logical_agent_id
               AND json_extract(d.request_json,'$.role')=NEW.role_id
               AND json_extract(d.request_json,'$.provider_id')=NEW.provider_id
               AND json_extract(d.request_json,'$.model')=NEW.model
               AND json_extract(d.request_json,'$.agent_id')=NEW.logical_agent_id
        )
        BEGIN SELECT RAISE(ABORT, 'planning pipeline invocation scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_artifact_scope_valid
        BEFORE INSERT ON autonomous_planning_pipeline_artifacts
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_pipeline_runs r
              JOIN autonomous_planning_manifests p
                ON p.id=r.manifest_id
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=r.mission_id
               AND h.source_id=p.specification_source_id
              JOIN autonomous_planning_pipeline_invocations i
                ON i.id=NEW.invocation_id AND i.run_id=r.id AND i.valid=1
             WHERE r.id=NEW.run_id AND r.manifest_id=NEW.manifest_id
               AND i.role_id=NEW.role_id
               AND i.invocation_order=NEW.invocation_order
        )
        BEGIN SELECT RAISE(ABORT, 'planning pipeline artifact scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_revision_source_valid
        BEFORE INSERT ON autonomous_backlog_revisions
        WHEN json_extract(
                 NEW.snapshot_json,'$.extension_schema'
             )='agentfactory.autonomous-planning/v1'
         AND NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_manifests p
              JOIN autonomous_planning_pipeline_runs run
                ON run.manifest_id=p.id
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=p.mission_id
               AND h.source_id=p.specification_source_id
              JOIN autonomous_mission_specification_sources s
                ON s.id=p.specification_source_id
             WHERE p.id=json_extract(
                       NEW.snapshot_json,'$.planning_contract.manifest_id'
                   )
               AND run.id=json_extract(
                       NEW.snapshot_json,'$.planning_contract.planning_run_id'
                   )
               AND run.proposal_key=json_extract(
                       NEW.snapshot_json,'$.planning_contract.proposal_key'
                   )
               AND run.planning_authorization_id=json_extract(
                       NEW.snapshot_json,
                       '$.planning_contract.planning_authorization_id'
                   )
               AND p.mission_id=NEW.mission_id
               AND p.manifest_digest=json_extract(
                       NEW.snapshot_json,'$.planning_contract.manifest_digest'
                   )
               AND s.raw_digest=NEW.source_sha256
        )
        BEGIN SELECT RAISE(ABORT, 'planning revision source scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_failure_scope_valid
        BEFORE INSERT ON autonomous_planning_pipeline_failures
        WHEN EXISTS (
            SELECT 1 FROM autonomous_planning_pipeline_completions c
             WHERE c.run_id=NEW.run_id
        ) OR NOT EXISTS (
            SELECT 1 FROM autonomous_planning_pipeline_runs r WHERE r.id=NEW.run_id
        )
        BEGIN SELECT RAISE(ABORT, 'planning pipeline failure scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_planning_completion_scope_valid
        BEFORE INSERT ON autonomous_planning_pipeline_completions
        WHEN EXISTS (
            SELECT 1 FROM autonomous_planning_pipeline_failures f
             WHERE f.run_id=NEW.run_id
        ) OR NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_pipeline_runs run
              JOIN autonomous_planning_manifests p
                ON p.id=run.manifest_id
              JOIN autonomous_mission_specification_heads h
                ON h.mission_id=run.mission_id
               AND h.source_id=p.specification_source_id
              JOIN autonomous_backlog_revisions r
                ON r.id=NEW.revision_id AND r.mission_id=run.mission_id
               AND r.revision_digest=NEW.revision_digest
              LEFT JOIN autonomous_backlog_revision_invalidations invalidation
                ON invalidation.revision_id=r.id
              JOIN autonomous_planning_manifest_revision_bindings b
                ON b.id=NEW.manifest_revision_binding_id
               AND b.manifest_id=p.id AND b.revision_id=r.id
              JOIN autonomous_planning_pipeline_artifacts a
                ON a.id=NEW.final_artifact_id AND a.run_id=run.id
               AND a.role_id='backlog_reviewer'
             WHERE run.id=NEW.run_id AND run.mission_id=NEW.mission_id
               AND run.manifest_id=NEW.manifest_id
               AND invalidation.id IS NULL
               AND (SELECT COUNT(*)
                      FROM autonomous_planning_pipeline_artifacts x
                     WHERE x.run_id=run.id)=5
        )
        BEGIN SELECT RAISE(ABORT, 'planning pipeline completion scope is invalid'); END;
    """),
    (64, """
        CREATE TABLE IF NOT EXISTS autonomous_proposal_verifications(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            pipeline_run_id INTEGER NOT NULL
                REFERENCES autonomous_planning_pipeline_runs(id),
            completion_id INTEGER NOT NULL
                REFERENCES autonomous_planning_pipeline_completions(id),
            completion_digest TEXT NOT NULL
                CHECK(length(completion_digest)=64
                      AND completion_digest NOT GLOB '*[^0-9a-f]*'),
            manifest_id INTEGER NOT NULL REFERENCES autonomous_planning_manifests(id),
            manifest_digest TEXT NOT NULL
                CHECK(length(manifest_digest)=64
                      AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
            revision_id INTEGER NOT NULL REFERENCES autonomous_backlog_revisions(id),
            revision_digest TEXT NOT NULL
                CHECK(length(revision_digest)=64
                      AND revision_digest NOT GLOB '*[^0-9a-f]*'),
            source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            source_digest TEXT NOT NULL
                CHECK(length(source_digest)=64
                      AND source_digest NOT GLOB '*[^0-9a-f]*'),
            verifier_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('READY','BLOCKED')),
            canonical_snapshot_json TEXT NOT NULL,
            canonical_digest TEXT NOT NULL
                CHECK(length(canonical_digest)=64
                      AND canonical_digest NOT GLOB '*[^0-9a-f]*'),
            presentation_json TEXT NOT NULL,
            presentation_digest TEXT NOT NULL
                CHECK(length(presentation_digest)=64
                      AND presentation_digest NOT GLOB '*[^0-9a-f]*'),
            checks_json TEXT NOT NULL,
            findings_json TEXT NOT NULL,
            reviewer_findings_json TEXT NOT NULL,
            human_visible_findings_json TEXT NOT NULL,
            verified_by TEXT NOT NULL,
            expected_mission_version INTEGER NOT NULL CHECK(expected_mission_version>0),
            mission_result_version INTEGER,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            report_digest TEXT NOT NULL
                CHECK(length(report_digest)=64
                      AND report_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            CHECK(
                (status='READY'
                 AND mission_result_version=expected_mission_version+1)
                OR (status='BLOCKED' AND mission_result_version IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_proposal_verifications
            ON autonomous_proposal_verifications(mission_id,id);
        CREATE INDEX IF NOT EXISTS idx_autonomous_proposal_verification_revision
            ON autonomous_proposal_verifications(revision_id,status,id);

        CREATE TRIGGER IF NOT EXISTS autonomous_proposal_verifications_no_update
        BEFORE UPDATE ON autonomous_proposal_verifications
        BEGIN SELECT RAISE(ABORT, 'proposal verifications are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_proposal_verifications_no_delete
        BEFORE DELETE ON autonomous_proposal_verifications
        BEGIN SELECT RAISE(ABORT, 'proposal verifications are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_proposal_verification_scope_valid
        BEFORE INSERT ON autonomous_proposal_verifications
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_planning_pipeline_runs run
              JOIN autonomous_planning_pipeline_completions completion
                ON completion.id=NEW.completion_id
               AND completion.run_id=run.id
               AND completion.completion_digest=NEW.completion_digest
              JOIN autonomous_planning_manifests manifest
                ON manifest.id=run.manifest_id
               AND manifest.manifest_digest=NEW.manifest_digest
              JOIN autonomous_planning_manifest_revision_bindings binding
                ON binding.id=completion.manifest_revision_binding_id
               AND binding.manifest_id=manifest.id
              JOIN autonomous_backlog_revisions revision
                ON revision.id=completion.revision_id
               AND revision.id=binding.revision_id
               AND revision.revision_digest=NEW.revision_digest
               AND revision.snapshot_json=NEW.canonical_snapshot_json
              JOIN autonomous_mission_specification_sources source
                ON source.id=manifest.specification_source_id
               AND source.source_digest=NEW.source_digest
             WHERE run.id=NEW.pipeline_run_id
               AND run.mission_id=NEW.mission_id
               AND manifest.id=NEW.manifest_id
               AND revision.mission_id=NEW.mission_id
               AND source.id=NEW.source_id
               AND NEW.revision_id=revision.id
               AND NEW.canonical_digest=revision.revision_digest
        )
        BEGIN SELECT RAISE(ABORT, 'proposal verification scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_ready_proposal_scope_valid
        BEFORE INSERT ON autonomous_proposal_verifications
        WHEN NEW.status='READY' AND NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_mission_specification_heads head
                ON head.mission_id=mission.id AND head.source_id=NEW.source_id
              JOIN autonomous_planning_manifests manifest
                ON manifest.id=NEW.manifest_id
               AND manifest.specification_source_id=head.source_id
              LEFT JOIN autonomous_backlog_revision_invalidations invalidation
                ON invalidation.revision_id=NEW.revision_id
             WHERE mission.id=NEW.mission_id
               AND mission.phase='BACKLOG_GENERATION'
               AND mission.disposition='RUNNING'
               AND mission.version=NEW.expected_mission_version
               AND invalidation.id IS NULL
        )
        BEGIN SELECT RAISE(ABORT, 'ready proposal scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_waiting_requires_verified_proposal
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN OLD.phase='BACKLOG_GENERATION'
         AND NEW.phase='WAITING_FOR_BACKLOG_APPROVAL'
         AND EXISTS (
            SELECT 1
              FROM autonomous_planning_pipeline_runs run
              JOIN autonomous_planning_pipeline_completions completion
                ON completion.run_id=run.id
             WHERE run.mission_id=OLD.id
         )
         AND NOT EXISTS (
            SELECT 1
              FROM autonomous_proposal_verifications verification
             WHERE verification.mission_id=OLD.id
               AND verification.status='READY'
               AND verification.mission_result_version=NEW.version
         )
        BEGIN SELECT RAISE(ABORT, 'verified proposal is required before approval wait'); END;
    """),
    (65, """
        CREATE TABLE IF NOT EXISTS autonomous_backlog_approvals(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            verification_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_proposal_verifications(id),
            pipeline_run_id INTEGER NOT NULL
                REFERENCES autonomous_planning_pipeline_runs(id),
            revision_id INTEGER NOT NULL REFERENCES autonomous_backlog_revisions(id),
            revision_digest TEXT NOT NULL
                CHECK(length(revision_digest)=64
                      AND revision_digest NOT GLOB '*[^0-9a-f]*'),
            canonical_digest TEXT NOT NULL
                CHECK(length(canonical_digest)=64
                      AND canonical_digest NOT GLOB '*[^0-9a-f]*'),
            source_id INTEGER NOT NULL
                REFERENCES autonomous_mission_specification_sources(id),
            source_digest TEXT NOT NULL
                CHECK(length(source_digest)=64
                      AND source_digest NOT GLOB '*[^0-9a-f]*'),
            planning_manifest_id INTEGER NOT NULL
                REFERENCES autonomous_planning_manifests(id),
            planning_manifest_digest TEXT NOT NULL
                CHECK(length(planning_manifest_digest)=64
                      AND planning_manifest_digest NOT GLOB '*[^0-9a-f]*'),
            planning_model_manifest_json TEXT NOT NULL,
            execution_role_model_manifest_json TEXT NOT NULL,
            execution_role_model_manifest_digest TEXT NOT NULL
                CHECK(length(execution_role_model_manifest_digest)=64
                      AND execution_role_model_manifest_digest NOT GLOB '*[^0-9a-f]*'),
            provider_ids_json TEXT NOT NULL,
            tool_manifest_json TEXT NOT NULL,
            tool_manifest_digest TEXT NOT NULL
                CHECK(length(tool_manifest_digest)=64
                      AND tool_manifest_digest NOT GLOB '*[^0-9a-f]*'),
            policy_version INTEGER NOT NULL CHECK(policy_version>0),
            policy_snapshot_json TEXT NOT NULL,
            policy_digest TEXT NOT NULL
                CHECK(length(policy_digest)=64
                      AND policy_digest NOT GLOB '*[^0-9a-f]*'),
            execution_epoch_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_execution_epochs(id),
            execution_authorization_digest TEXT NOT NULL UNIQUE
                CHECK(length(execution_authorization_digest)=64
                      AND execution_authorization_digest NOT GLOB '*[^0-9a-f]*'),
            approved_by TEXT NOT NULL,
            authentication_context_json TEXT NOT NULL,
            authentication_context_digest TEXT NOT NULL
                CHECK(length(authentication_context_digest)=64
                      AND authentication_context_digest NOT GLOB '*[^0-9a-f]*'),
            expected_mission_version INTEGER NOT NULL CHECK(expected_mission_version>0),
            result_mission_version INTEGER NOT NULL
                CHECK(result_mission_version=expected_mission_version+1),
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            reason TEXT NOT NULL,
            approval_digest TEXT NOT NULL UNIQUE
                CHECK(length(approval_digest)=64
                      AND approval_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_backlog_approvals
            ON autonomous_backlog_approvals(mission_id,id);

        CREATE TABLE IF NOT EXISTS autonomous_backlog_approval_completions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            approval_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_approvals(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            authorization_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_local_authorizations(id),
            result_mission_version INTEGER NOT NULL CHECK(result_mission_version>0),
            completion_digest TEXT NOT NULL UNIQUE
                CHECK(length(completion_digest)=64
                      AND completion_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
        );

        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approvals_no_update
        BEFORE UPDATE ON autonomous_backlog_approvals
        BEGIN SELECT RAISE(ABORT, 'backlog approvals are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approvals_no_delete
        BEFORE DELETE ON autonomous_backlog_approvals
        BEGIN SELECT RAISE(ABORT, 'backlog approvals are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approval_completions_no_update
        BEFORE UPDATE ON autonomous_backlog_approval_completions
        BEGIN SELECT RAISE(ABORT, 'backlog approval completions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approval_completions_no_delete
        BEFORE DELETE ON autonomous_backlog_approval_completions
        BEGIN SELECT RAISE(ABORT, 'backlog approval completions are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approval_scope_valid
        BEFORE INSERT ON autonomous_backlog_approvals
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_proposal_verifications verification
                ON verification.id=NEW.verification_id
               AND verification.mission_id=mission.id
               AND verification.status='READY'
               AND verification.pipeline_run_id=NEW.pipeline_run_id
               AND verification.revision_id=NEW.revision_id
               AND verification.revision_digest=NEW.revision_digest
               AND verification.canonical_digest=NEW.canonical_digest
               AND verification.source_id=NEW.source_id
               AND verification.source_digest=NEW.source_digest
               AND verification.manifest_id=NEW.planning_manifest_id
               AND verification.manifest_digest=NEW.planning_manifest_digest
               AND verification.mission_result_version=NEW.expected_mission_version
              JOIN autonomous_mission_specification_heads head
                ON head.mission_id=mission.id AND head.source_id=NEW.source_id
              JOIN autonomous_planning_manifests manifest
                ON manifest.id=NEW.planning_manifest_id
               AND manifest.mission_id=mission.id
               AND manifest.specification_source_id=head.source_id
              JOIN autonomous_backlog_revisions revision
                ON revision.id=NEW.revision_id
               AND revision.mission_id=mission.id
               AND revision.revision_digest=NEW.revision_digest
              JOIN autonomous_mission_execution_epochs epoch
                ON epoch.id=NEW.execution_epoch_id
               AND epoch.mission_id=mission.id
               AND epoch.epoch_number=1
               AND epoch.base_backlog_revision_id=revision.id
               AND epoch.base_backlog_revision_digest=revision.revision_digest
               AND epoch.activation_mission_version=NEW.result_mission_version
              LEFT JOIN autonomous_backlog_revision_invalidations invalidation
                ON invalidation.revision_id=revision.id
             WHERE mission.id=NEW.mission_id
               AND mission.mission_owner=NEW.approved_by
               AND mission.phase='WAITING_FOR_BACKLOG_APPROVAL'
               AND mission.disposition='RUNNING'
               AND mission.version=NEW.expected_mission_version
               AND mission.active_backlog_revision_id IS NULL
               AND mission.active_execution_epoch_id IS NULL
               AND invalidation.id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_backlog_revisions later
                    WHERE later.mission_id=mission.id
                      AND later.revision_number>revision.revision_number
               )
        )
        BEGIN SELECT RAISE(ABORT, 'backlog approval scope is invalid'); END;

        DROP TRIGGER autonomous_local_authorization_scope_valid;
        CREATE TRIGGER autonomous_local_authorization_scope_valid
        BEFORE INSERT ON autonomous_local_authorizations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_backlog_revisions revision
                ON revision.id=NEW.backlog_revision_id
               AND revision.mission_id=mission.id
              JOIN autonomous_mission_execution_epochs epoch
                ON epoch.id=NEW.execution_epoch_id
               AND epoch.mission_id=mission.id
             WHERE mission.id=NEW.mission_id
               AND mission.active_backlog_revision_id=NEW.backlog_revision_id
               AND mission.active_execution_epoch_id=NEW.execution_epoch_id
               AND revision.revision_digest=NEW.backlog_revision_digest
               AND epoch.epoch_branch=NEW.epoch_branch
        ) AND NOT EXISTS (
            SELECT 1
              FROM autonomous_backlog_approvals approval
              JOIN autonomous_mission_execution_epochs epoch
                ON epoch.id=approval.execution_epoch_id
             WHERE approval.mission_id=NEW.mission_id
               AND approval.revision_id=NEW.backlog_revision_id
               AND approval.revision_digest=NEW.backlog_revision_digest
               AND approval.execution_epoch_id=NEW.execution_epoch_id
               AND approval.execution_authorization_digest=NEW.authorization_digest
               AND epoch.epoch_branch=NEW.epoch_branch
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous authorization scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_backlog_approval_completion_scope_valid
        BEFORE INSERT ON autonomous_backlog_approval_completions
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_backlog_approvals approval
              JOIN autonomous_local_authorizations authorization
                ON authorization.id=NEW.authorization_id
               AND authorization.mission_id=approval.mission_id
               AND authorization.backlog_revision_id=approval.revision_id
               AND authorization.backlog_revision_digest=approval.revision_digest
               AND authorization.execution_epoch_id=approval.execution_epoch_id
               AND authorization.authorization_digest=
                   approval.execution_authorization_digest
             WHERE approval.id=NEW.approval_id
               AND approval.mission_id=NEW.mission_id
               AND approval.result_mission_version=NEW.result_mission_version
        )
        BEGIN SELECT RAISE(ABORT, 'backlog approval completion scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_approved_requires_exact_approval
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN OLD.phase='WAITING_FOR_BACKLOG_APPROVAL'
         AND NEW.phase='APPROVED'
         AND EXISTS (
            SELECT 1 FROM autonomous_proposal_verifications verification
             WHERE verification.mission_id=OLD.id
         )
         AND NOT EXISTS (
            SELECT 1
              FROM autonomous_backlog_approval_completions completion
              JOIN autonomous_backlog_approvals approval
                ON approval.id=completion.approval_id
              JOIN autonomous_local_authorizations authorization
                ON authorization.id=completion.authorization_id
             WHERE completion.mission_id=OLD.id
               AND completion.result_mission_version=NEW.version
               AND approval.revision_id=NEW.active_backlog_revision_id
               AND approval.execution_epoch_id=NEW.active_execution_epoch_id
               AND authorization.backlog_revision_id=NEW.active_backlog_revision_id
               AND authorization.execution_epoch_id=NEW.active_execution_epoch_id
        )
        BEGIN SELECT RAISE(ABORT, 'exact backlog approval is required'); END;

        CREATE TABLE IF NOT EXISTS autonomous_backlog_revision_authorities(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            revision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_revisions(id),
            revision_origin TEXT NOT NULL CHECK(revision_origin IN
                ('HUMAN','AGENT_MATERIAL','TECHNICAL_SUBTASK')),
            parent_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            base_approval_id INTEGER
                REFERENCES autonomous_backlog_approvals(id),
            base_authority_id INTEGER
                REFERENCES autonomous_backlog_revision_authorities(id),
            approved_item_stable_id TEXT,
            approved_item_digest TEXT
                CHECK(approved_item_digest IS NULL OR
                      (length(approved_item_digest)=64
                       AND approved_item_digest NOT GLOB '*[^0-9a-f]*')),
            outcome TEXT NOT NULL CHECK(outcome IN
                ('APPLIED','WAITING_FOR_APPROVAL')),
            authenticated_actor TEXT NOT NULL,
            authentication_context_json TEXT NOT NULL,
            authentication_context_digest TEXT NOT NULL
                CHECK(length(authentication_context_digest)=64
                      AND authentication_context_digest NOT GLOB '*[^0-9a-f]*'),
            expected_mission_version INTEGER NOT NULL
                CHECK(expected_mission_version>0),
            result_mission_version INTEGER NOT NULL
                CHECK(result_mission_version=expected_mission_version+1),
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            reason TEXT NOT NULL,
            authority_digest TEXT NOT NULL UNIQUE
                CHECK(length(authority_digest)=64
                      AND authority_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            CHECK((base_approval_id IS NULL)<>(base_authority_id IS NULL)),
            CHECK(
                (revision_origin='AGENT_MATERIAL'
                 AND outcome='WAITING_FOR_APPROVAL'
                 AND approved_item_stable_id IS NULL
                 AND approved_item_digest IS NULL) OR
                (revision_origin='HUMAN'
                 AND outcome='APPLIED'
                 AND approved_item_stable_id IS NULL
                 AND approved_item_digest IS NULL) OR
                (revision_origin='TECHNICAL_SUBTASK'
                 AND outcome='APPLIED'
                 AND approved_item_stable_id IS NOT NULL
                 AND approved_item_digest IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_autonomous_revision_authorities
            ON autonomous_backlog_revision_authorities(mission_id,id);

        CREATE TRIGGER IF NOT EXISTS autonomous_revision_authorities_no_update
        BEFORE UPDATE ON autonomous_backlog_revision_authorities
        BEGIN SELECT RAISE(ABORT, 'revision authorities are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS autonomous_revision_authorities_no_delete
        BEFORE DELETE ON autonomous_backlog_revision_authorities
        BEGIN SELECT RAISE(ABORT, 'revision authorities are immutable'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_revision_authority_scope_valid
        BEFORE INSERT ON autonomous_backlog_revision_authorities
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_backlog_revisions revision
              JOIN autonomous_missions mission
                ON mission.id=revision.mission_id
              LEFT JOIN autonomous_backlog_items approved_item
                ON approved_item.revision_id=NEW.parent_revision_id
               AND approved_item.stable_id=NEW.approved_item_stable_id
             WHERE revision.id=NEW.revision_id
               AND revision.mission_id=NEW.mission_id
               AND revision.origin=NEW.revision_origin
               AND revision.parent_revision_id=NEW.parent_revision_id
               AND mission.active_backlog_revision_id=NEW.parent_revision_id
               AND mission.version=NEW.expected_mission_version
               AND mission.disposition='RUNNING'
               AND mission.phase IN (
                   'APPROVED','ENVIRONMENT_DISCOVERY','ENVIRONMENT_BOOTSTRAP',
                   'DEVELOPMENT','VALIDATION','INTEGRATION','FINAL_VALIDATION'
               )
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_backlog_revision_invalidations invalid
                    WHERE invalid.revision_id=revision.id
               )
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_backlog_revisions later
                    WHERE later.mission_id=mission.id
                      AND later.revision_number>revision.revision_number
               )
               AND (
                   (NEW.base_approval_id IS NOT NULL AND EXISTS (
                       SELECT 1
                         FROM autonomous_backlog_approvals approval
                         JOIN autonomous_backlog_approval_completions completion
                           ON completion.approval_id=approval.id
                        WHERE approval.id=NEW.base_approval_id
                          AND approval.mission_id=mission.id
                          AND approval.revision_id=NEW.parent_revision_id
                   )) OR
                   (NEW.base_authority_id IS NOT NULL AND EXISTS (
                       SELECT 1
                         FROM autonomous_backlog_revision_authorities base
                        WHERE base.id=NEW.base_authority_id
                          AND base.mission_id=mission.id
                          AND base.revision_id=NEW.parent_revision_id
                          AND base.outcome='APPLIED'
                   ))
               )
               AND (
                   (NEW.revision_origin='HUMAN'
                    AND NEW.authenticated_actor=mission.mission_owner) OR
                   (NEW.revision_origin='AGENT_MATERIAL'
                    AND NEW.authenticated_actor=revision.created_by) OR
                   (NEW.revision_origin='TECHNICAL_SUBTASK'
                    AND NEW.authenticated_actor=revision.created_by
                    AND approved_item.id IS NOT NULL
                    AND approved_item.executable=1
                    AND approved_item.item_digest=NEW.approved_item_digest)
               )
        )
        BEGIN SELECT RAISE(ABORT, 'revision authority scope is invalid'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_revision_activation_requires_authority
        BEFORE UPDATE OF active_backlog_revision_id ON autonomous_missions
        WHEN COALESCE(NEW.active_backlog_revision_id,-1)<>
             COALESCE(OLD.active_backlog_revision_id,-1)
         AND EXISTS (
             SELECT 1 FROM autonomous_proposal_verifications verification
              WHERE verification.mission_id=OLD.id
         )
         AND NOT EXISTS (
             SELECT 1
               FROM autonomous_backlog_approvals approval
               JOIN autonomous_backlog_approval_completions completion
                 ON completion.approval_id=approval.id
              WHERE approval.mission_id=OLD.id
                AND approval.revision_id=NEW.active_backlog_revision_id
                AND completion.result_mission_version=NEW.version
         )
         AND NOT EXISTS (
             SELECT 1 FROM autonomous_backlog_revision_authorities authority
              WHERE authority.mission_id=OLD.id
                AND authority.revision_id=NEW.active_backlog_revision_id
                AND authority.outcome='APPLIED'
                AND authority.result_mission_version=NEW.version
         )
        BEGIN SELECT RAISE(ABORT, 'backlog revision activation lacks authority'); END;

        CREATE TRIGGER IF NOT EXISTS autonomous_material_wait_requires_authority
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN NEW.phase='WAITING_FOR_BACKLOG_APPROVAL'
         AND OLD.phase IN (
             'APPROVED','ENVIRONMENT_DISCOVERY','ENVIRONMENT_BOOTSTRAP',
             'DEVELOPMENT','VALIDATION','INTEGRATION','FINAL_VALIDATION'
         )
         AND NOT EXISTS (
             SELECT 1 FROM autonomous_backlog_revision_authorities authority
              WHERE authority.mission_id=OLD.id
                AND authority.revision_origin='AGENT_MATERIAL'
                AND authority.outcome='WAITING_FOR_APPROVAL'
                AND authority.result_mission_version=NEW.version
         )
        BEGIN SELECT RAISE(ABORT, 'material revision authority is required'); END;

        DROP TRIGGER autonomous_missions_phase_transition;
        CREATE TRIGGER autonomous_missions_phase_transition
        BEFORE UPDATE OF phase ON autonomous_missions
        WHEN NEW.phase<>OLD.phase AND NOT (
            (OLD.phase='DRAFT' AND NEW.phase='SPECIFICATION_ANALYSIS') OR
            (OLD.phase='SPECIFICATION_ANALYSIS' AND NEW.phase='BACKLOG_GENERATION') OR
            (OLD.phase='BACKLOG_GENERATION' AND NEW.phase IN
                ('SPECIFICATION_ANALYSIS','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='WAITING_FOR_BACKLOG_APPROVAL' AND NEW.phase IN
                ('BACKLOG_GENERATION','APPROVED')) OR
            (OLD.phase='APPROVED' AND NEW.phase IN
                ('ENVIRONMENT_DISCOVERY','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='ENVIRONMENT_DISCOVERY' AND NEW.phase IN
                ('ENVIRONMENT_BOOTSTRAP','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='ENVIRONMENT_BOOTSTRAP' AND NEW.phase IN
                ('DEVELOPMENT','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='DEVELOPMENT' AND NEW.phase IN
                ('VALIDATION','FINAL_VALIDATION','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='VALIDATION' AND NEW.phase IN
                ('DEVELOPMENT','INTEGRATION','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='INTEGRATION' AND NEW.phase IN
                ('DEVELOPMENT','FINAL_VALIDATION','WAITING_FOR_BACKLOG_APPROVAL')) OR
            (OLD.phase='FINAL_VALIDATION' AND NEW.phase IN
                ('DEVELOPMENT','COMPLETED','WAITING_FOR_BACKLOG_APPROVAL'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid autonomous mission phase transition'); END;
    """),
    (66, """
        CREATE TABLE autonomous_child_jobs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            backlog_revision_digest TEXT NOT NULL
                CHECK(length(backlog_revision_digest)=64
                      AND backlog_revision_digest NOT GLOB '*[^0-9a-f]*'),
            execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            authorization_id INTEGER NOT NULL
                REFERENCES autonomous_local_authorizations(id),
            backlog_item_id INTEGER NOT NULL REFERENCES autonomous_backlog_items(id),
            stable_item_id TEXT NOT NULL,
            item_digest TEXT NOT NULL
                CHECK(length(item_digest)=64
                      AND item_digest NOT GLOB '*[^0-9a-f]*'),
            logical_attempt INTEGER NOT NULL CHECK(logical_attempt>0),
            task_id INTEGER NOT NULL UNIQUE REFERENCES work_items(id),
            run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            child_workflow_id TEXT NOT NULL UNIQUE,
            workflow_definition_id TEXT NOT NULL,
            execution_mode TEXT NOT NULL CHECK(execution_mode IN ('simulation','live')),
            context_json TEXT NOT NULL,
            context_digest TEXT NOT NULL UNIQUE
                CHECK(length(context_digest)=64
                      AND context_digest NOT GLOB '*[^0-9a-f]*'),
            prepared_by TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL,
            UNIQUE(mission_id,backlog_revision_id,execution_epoch_id,
                   stable_item_id,logical_attempt)
        );
        CREATE INDEX idx_autonomous_child_jobs_mission
            ON autonomous_child_jobs(mission_id,id);

        CREATE TABLE autonomous_child_job_authorizations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            child_job_id INTEGER NOT NULL UNIQUE REFERENCES autonomous_child_jobs(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            authorization_id INTEGER NOT NULL
                REFERENCES autonomous_local_authorizations(id),
            decision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_authorization_decisions(id),
            decision_digest TEXT NOT NULL UNIQUE
                CHECK(length(decision_digest)=64
                      AND decision_digest NOT GLOB '*[^0-9a-f]*'),
            provider_id TEXT NOT NULL,
            role TEXT NOT NULL,
            model TEXT NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomous_child_delivery_completions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            child_job_id INTEGER NOT NULL UNIQUE REFERENCES autonomous_child_jobs(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id),
            authorization_decision_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_authorization_decisions(id),
            stage_evidence_json TEXT NOT NULL,
            stage_evidence_digest TEXT NOT NULL
                CHECK(length(stage_evidence_digest)=64
                      AND stage_evidence_digest NOT GLOB '*[^0-9a-f]*'),
            validation_evidence_json TEXT NOT NULL,
            validation_evidence_digest TEXT NOT NULL
                CHECK(length(validation_evidence_digest)=64
                      AND validation_evidence_digest NOT GLOB '*[^0-9a-f]*'),
            review_evidence_json TEXT NOT NULL,
            review_evidence_digest TEXT NOT NULL
                CHECK(length(review_evidence_digest)=64
                      AND review_evidence_digest NOT GLOB '*[^0-9a-f]*'),
            integration_evidence_json TEXT NOT NULL,
            integration_evidence_digest TEXT NOT NULL
                CHECK(length(integration_evidence_digest)=64
                      AND integration_evidence_digest NOT GLOB '*[^0-9a-f]*'),
            git_commit_sha TEXT NOT NULL,
            completion_digest TEXT NOT NULL UNIQUE
                CHECK(length(completion_digest)=64
                      AND completion_digest NOT GLOB '*[^0-9a-f]*'),
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE autonomous_child_reconciliations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            child_job_id INTEGER NOT NULL UNIQUE REFERENCES autonomous_child_jobs(id),
            completion_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_child_delivery_completions(id),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            backlog_item_state_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_backlog_item_states(id),
            checkpoint_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_checkpoints(id),
            result_mission_version INTEGER NOT NULL CHECK(result_mission_version>0),
            reconciliation_digest TEXT NOT NULL UNIQUE
                CHECK(length(reconciliation_digest)=64
                      AND reconciliation_digest NOT GLOB '*[^0-9a-f]*'),
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TRIGGER autonomous_child_jobs_no_update
        BEFORE UPDATE ON autonomous_child_jobs
        BEGIN SELECT RAISE(ABORT, 'autonomous child jobs are immutable'); END;
        CREATE TRIGGER autonomous_child_jobs_no_delete
        BEFORE DELETE ON autonomous_child_jobs
        BEGIN SELECT RAISE(ABORT, 'autonomous child jobs are immutable'); END;
        CREATE TRIGGER autonomous_child_job_authorizations_no_update
        BEFORE UPDATE ON autonomous_child_job_authorizations
        BEGIN SELECT RAISE(ABORT, 'autonomous child authorizations are immutable'); END;
        CREATE TRIGGER autonomous_child_job_authorizations_no_delete
        BEFORE DELETE ON autonomous_child_job_authorizations
        BEGIN SELECT RAISE(ABORT, 'autonomous child authorizations are immutable'); END;
        CREATE TRIGGER autonomous_child_completions_no_update
        BEFORE UPDATE ON autonomous_child_delivery_completions
        BEGIN SELECT RAISE(ABORT, 'autonomous child completions are immutable'); END;
        CREATE TRIGGER autonomous_child_completions_no_delete
        BEFORE DELETE ON autonomous_child_delivery_completions
        BEGIN SELECT RAISE(ABORT, 'autonomous child completions are immutable'); END;
        CREATE TRIGGER autonomous_child_reconciliations_no_update
        BEFORE UPDATE ON autonomous_child_reconciliations
        BEGIN SELECT RAISE(ABORT, 'autonomous child reconciliations are immutable'); END;
        CREATE TRIGGER autonomous_child_reconciliations_no_delete
        BEFORE DELETE ON autonomous_child_reconciliations
        BEGIN SELECT RAISE(ABORT, 'autonomous child reconciliations are immutable'); END;

        CREATE TRIGGER autonomous_child_job_scope_valid
        BEFORE INSERT ON autonomous_child_jobs
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_backlog_revisions revision
                ON revision.id=mission.active_backlog_revision_id
              JOIN autonomous_mission_execution_epochs epoch
                ON epoch.id=mission.active_execution_epoch_id
              JOIN autonomous_local_authorizations authorization
                ON authorization.id=NEW.authorization_id
               AND authorization.mission_id=mission.id
               AND authorization.backlog_revision_id=revision.id
               AND authorization.execution_epoch_id=epoch.id
              JOIN autonomous_backlog_items item
                ON item.id=NEW.backlog_item_id
               AND item.revision_id=revision.id
               AND item.stable_id=NEW.stable_item_id
               AND item.item_digest=NEW.item_digest
               AND item.executable=1
              JOIN workflow_runs run
                ON run.id=NEW.run_id AND run.task_id=NEW.task_id
             WHERE mission.id=NEW.mission_id
               AND mission.phase='DEVELOPMENT'
               AND mission.disposition='RUNNING'
               AND revision.id=NEW.backlog_revision_id
               AND revision.revision_digest=NEW.backlog_revision_digest
               AND epoch.id=NEW.execution_epoch_id
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_authorization_revocations revoked
                    WHERE revoked.authorization_id=authorization.id
               )
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous child job scope is invalid'); END;

        CREATE TRIGGER autonomous_child_authorization_scope_valid
        BEFORE INSERT ON autonomous_child_job_authorizations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_child_jobs job
              JOIN autonomous_authorization_decisions decision
                ON decision.id=NEW.decision_id
               AND decision.mission_id=job.mission_id
               AND decision.outcome='ALLOW_AUTONOMOUS'
               AND decision.authority_valid=1
               AND decision.autonomous_authorization_id=job.authorization_id
             WHERE job.id=NEW.child_job_id
               AND job.mission_id=NEW.mission_id
               AND job.authorization_id=NEW.authorization_id
               AND decision.decision_digest=NEW.decision_digest
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous child authorization is invalid'); END;

        CREATE TRIGGER autonomous_child_completion_scope_valid
        BEFORE INSERT ON autonomous_child_delivery_completions
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_child_jobs job
              JOIN autonomous_child_job_authorizations authorization
                ON authorization.child_job_id=job.id
               AND authorization.decision_id=NEW.authorization_decision_id
             WHERE job.id=NEW.child_job_id
               AND job.mission_id=NEW.mission_id
               AND job.run_id=NEW.run_id
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous child completion scope is invalid'); END;

        CREATE TRIGGER autonomous_child_reconciliation_scope_valid
        BEFORE INSERT ON autonomous_child_reconciliations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_child_jobs job
              JOIN autonomous_child_delivery_completions completion
                ON completion.id=NEW.completion_id
               AND completion.child_job_id=job.id
              JOIN autonomous_backlog_item_states state
                ON state.id=NEW.backlog_item_state_id
               AND state.item_id=job.backlog_item_id
               AND state.status='DONE'
              JOIN autonomous_mission_checkpoints checkpoint
                ON checkpoint.id=NEW.checkpoint_id
               AND checkpoint.mission_id=job.mission_id
               AND checkpoint.execution_epoch_id=job.execution_epoch_id
               AND checkpoint.backlog_revision_id=job.backlog_revision_id
             WHERE job.id=NEW.child_job_id
               AND job.mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous child reconciliation is invalid'); END;
    """),
    (67, """
        ALTER TABLE autonomous_child_jobs
            ADD COLUMN control_fencing_token INTEGER NOT NULL DEFAULT 1
                CHECK(control_fencing_token>0);

        CREATE TABLE autonomous_mission_control_fences(
            mission_id INTEGER PRIMARY KEY REFERENCES autonomous_missions(id),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>0),
            mission_version INTEGER NOT NULL CHECK(mission_version>0),
            phase TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK(disposition IN (
                'RUNNING','PAUSED','STOPPED','NEEDS_ATTENTION',
                'NEEDS_HUMAN_ACTION','REPLANNING','RECOVERING','FAILED'
            )),
            backlog_revision_id INTEGER REFERENCES autonomous_backlog_revisions(id),
            execution_epoch_id INTEGER REFERENCES autonomous_mission_execution_epochs(id),
            updated_by_command_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO autonomous_mission_control_fences(
            mission_id,fencing_token,mission_version,phase,disposition,
            backlog_revision_id,execution_epoch_id,updated_by_command_id
        )
        SELECT id,1,version,phase,disposition,active_backlog_revision_id,
               active_execution_epoch_id,'migration-67:' || id
          FROM autonomous_missions;

        CREATE TABLE autonomous_mission_control_commands(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL CHECK(action IN (
                'PAUSE','RESUME','STOP','RETRY_CURRENT_TASK'
            )),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            expected_mission_version INTEGER NOT NULL CHECK(expected_mission_version>0),
            expected_fencing_token INTEGER NOT NULL CHECK(expected_fencing_token>0),
            expected_backlog_revision_id INTEGER REFERENCES autonomous_backlog_revisions(id),
            expected_execution_epoch_id INTEGER REFERENCES autonomous_mission_execution_epochs(id),
            child_job_id INTEGER REFERENCES autonomous_child_jobs(id),
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            result_mission_version INTEGER NOT NULL CHECK(result_mission_version>0),
            result_fencing_token INTEGER NOT NULL CHECK(result_fencing_token>0),
            result_phase TEXT NOT NULL,
            result_disposition TEXT NOT NULL,
            result_logical_attempt INTEGER CHECK(result_logical_attempt IS NULL OR result_logical_attempt>0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_autonomous_control_commands_mission
            ON autonomous_mission_control_commands(mission_id,id);

        CREATE TABLE autonomous_mission_operation_leases(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            execution_epoch_id INTEGER REFERENCES autonomous_mission_execution_epochs(id),
            child_job_id INTEGER REFERENCES autonomous_child_jobs(id),
            operation_kind TEXT NOT NULL CHECK(operation_kind IN (
                'INFERENCE','COMMAND','INSTALLATION','SERVICE_OPERATION',
                'NEXT_WORK_ITEM','WORKER_TOOL'
            )),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>0),
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            status TEXT NOT NULL CHECK(status IN (
                'ACTIVE','RELEASING','FINISHED','RELEASED'
            )),
            release_reason TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            terminal_at TEXT
        );
        CREATE INDEX idx_autonomous_operation_leases_active
            ON autonomous_mission_operation_leases(mission_id,status,id);

        CREATE TABLE autonomous_mission_retry_requests(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            child_job_id INTEGER NOT NULL UNIQUE REFERENCES autonomous_child_jobs(id),
            backlog_item_id INTEGER NOT NULL REFERENCES autonomous_backlog_items(id),
            stable_item_id TEXT NOT NULL,
            previous_logical_attempt INTEGER NOT NULL CHECK(previous_logical_attempt>0),
            next_logical_attempt INTEGER NOT NULL CHECK(next_logical_attempt=previous_logical_attempt+1),
            control_command_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_control_commands(id),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_autonomous_retry_requests_mission
            ON autonomous_mission_retry_requests(mission_id,id);

        CREATE TABLE autonomous_mission_retry_settlements(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            retry_request_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_retry_requests(id),
            child_job_id INTEGER NOT NULL UNIQUE REFERENCES autonomous_child_jobs(id),
            failed_state_id INTEGER NOT NULL REFERENCES autonomous_backlog_item_states(id),
            ready_state_id INTEGER NOT NULL REFERENCES autonomous_backlog_item_states(id),
            command_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TRIGGER autonomous_control_fences_no_delete
        BEFORE DELETE ON autonomous_mission_control_fences
        BEGIN SELECT RAISE(ABORT, 'mission control fences are durable'); END;
        CREATE TRIGGER autonomous_control_fence_valid_update
        BEFORE UPDATE ON autonomous_mission_control_fences
        WHEN NEW.mission_id<>OLD.mission_id
          OR NEW.fencing_token NOT IN (OLD.fencing_token,OLD.fencing_token+1)
          OR NEW.mission_version<OLD.mission_version
          OR (NEW.disposition<>OLD.disposition
              AND NEW.fencing_token<>OLD.fencing_token+1)
          OR (COALESCE(NEW.execution_epoch_id,-1)<>
              COALESCE(OLD.execution_epoch_id,-1)
              AND NEW.fencing_token<>OLD.fencing_token+1)
        BEGIN SELECT RAISE(ABORT, 'invalid mission control fence update'); END;
        CREATE TRIGGER autonomous_control_fence_scope_valid
        BEFORE UPDATE ON autonomous_mission_control_fences
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_missions mission
             WHERE mission.id=NEW.mission_id
               AND mission.version=NEW.mission_version
               AND mission.phase=NEW.phase
               AND mission.disposition=NEW.disposition
               AND COALESCE(mission.active_backlog_revision_id,-1)=
                   COALESCE(NEW.backlog_revision_id,-1)
               AND COALESCE(mission.active_execution_epoch_id,-1)=
                   COALESCE(NEW.execution_epoch_id,-1)
        )
        BEGIN SELECT RAISE(ABORT, 'mission control fence scope is stale'); END;

        CREATE TRIGGER autonomous_control_commands_no_update
        BEFORE UPDATE ON autonomous_mission_control_commands
        BEGIN SELECT RAISE(ABORT, 'mission control commands are immutable'); END;
        CREATE TRIGGER autonomous_control_commands_no_delete
        BEFORE DELETE ON autonomous_mission_control_commands
        BEGIN SELECT RAISE(ABORT, 'mission control commands are immutable'); END;

        CREATE TRIGGER autonomous_child_job_control_fence_valid
        BEFORE INSERT ON autonomous_child_jobs
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_mission_control_fences fence
             WHERE fence.mission_id=NEW.mission_id
               AND fence.disposition='RUNNING'
               AND fence.fencing_token=NEW.control_fencing_token
               AND COALESCE(fence.execution_epoch_id,-1)=
                   COALESCE(NEW.execution_epoch_id,-1)
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous child job control fence is stale'); END;

        CREATE TRIGGER autonomous_operation_lease_scope_valid
        BEFORE INSERT ON autonomous_mission_operation_leases
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_mission_control_fences fence
                ON fence.mission_id=mission.id
             WHERE mission.id=NEW.mission_id
               AND mission.disposition='RUNNING'
               AND fence.disposition='RUNNING'
               AND fence.fencing_token=NEW.fencing_token
               AND COALESCE(mission.active_execution_epoch_id,-1)=
                   COALESCE(NEW.execution_epoch_id,-1)
               AND (NEW.child_job_id IS NULL OR EXISTS (
                   SELECT 1 FROM autonomous_child_jobs child
                    WHERE child.id=NEW.child_job_id
                      AND child.mission_id=NEW.mission_id
                      AND child.execution_epoch_id=NEW.execution_epoch_id
               ))
        )
        BEGIN SELECT RAISE(ABORT, 'mission operation is fenced'); END;
        CREATE TRIGGER autonomous_operation_lease_scope_immutable
        BEFORE UPDATE ON autonomous_mission_operation_leases
        WHEN NEW.identity<>OLD.identity OR NEW.operation_id<>OLD.operation_id
          OR NEW.mission_id<>OLD.mission_id
          OR COALESCE(NEW.execution_epoch_id,-1)<>
             COALESCE(OLD.execution_epoch_id,-1)
          OR COALESCE(NEW.child_job_id,-1)<>COALESCE(OLD.child_job_id,-1)
          OR NEW.operation_kind<>OLD.operation_kind
          OR NEW.fencing_token<>OLD.fencing_token
          OR NEW.request_digest<>OLD.request_digest
          OR NEW.started_at<>OLD.started_at
        BEGIN SELECT RAISE(ABORT, 'mission operation lease scope is immutable'); END;
        CREATE TRIGGER autonomous_operation_lease_valid_transition
        BEFORE UPDATE OF status ON autonomous_mission_operation_leases
        WHEN NEW.status<>OLD.status AND NOT (
            (OLD.status='ACTIVE' AND NEW.status IN
                ('RELEASING','FINISHED','RELEASED')) OR
            (OLD.status='RELEASING' AND NEW.status='RELEASED')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid mission operation lease transition'); END;
        CREATE TRIGGER autonomous_operation_leases_no_delete
        BEFORE DELETE ON autonomous_mission_operation_leases
        BEGIN SELECT RAISE(ABORT, 'mission operation leases are durable'); END;

        CREATE TRIGGER autonomous_retry_requests_no_update
        BEFORE UPDATE ON autonomous_mission_retry_requests
        BEGIN SELECT RAISE(ABORT, 'mission retry requests are immutable'); END;
        CREATE TRIGGER autonomous_retry_requests_no_delete
        BEFORE DELETE ON autonomous_mission_retry_requests
        BEGIN SELECT RAISE(ABORT, 'mission retry requests are immutable'); END;
        CREATE TRIGGER autonomous_retry_request_scope_valid
        BEFORE INSERT ON autonomous_mission_retry_requests
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_child_jobs child
              JOIN autonomous_mission_control_commands command
                ON command.id=NEW.control_command_id
             WHERE child.id=NEW.child_job_id
               AND child.mission_id=NEW.mission_id
               AND child.backlog_item_id=NEW.backlog_item_id
               AND child.stable_item_id=NEW.stable_item_id
               AND child.logical_attempt=NEW.previous_logical_attempt
               AND command.mission_id=NEW.mission_id
               AND command.action='RETRY_CURRENT_TASK'
               AND command.result_fencing_token=NEW.fencing_token
        )
        BEGIN SELECT RAISE(ABORT, 'mission retry request scope is invalid'); END;
        CREATE TRIGGER autonomous_retry_settlements_no_update
        BEFORE UPDATE ON autonomous_mission_retry_settlements
        BEGIN SELECT RAISE(ABORT, 'mission retry settlements are immutable'); END;
        CREATE TRIGGER autonomous_retry_settlements_no_delete
        BEFORE DELETE ON autonomous_mission_retry_settlements
        BEGIN SELECT RAISE(ABORT, 'mission retry settlements are immutable'); END;
        CREATE TRIGGER autonomous_retry_settlement_scope_valid
        BEFORE INSERT ON autonomous_mission_retry_settlements
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_retry_requests retry
              JOIN autonomous_backlog_item_states failed
                ON failed.id=NEW.failed_state_id
              JOIN autonomous_backlog_item_states ready
                ON ready.id=NEW.ready_state_id
             WHERE retry.id=NEW.retry_request_id
               AND retry.child_job_id=NEW.child_job_id
               AND failed.item_id=retry.backlog_item_id
               AND failed.status='FAILED'
               AND ready.item_id=retry.backlog_item_id
               AND ready.status='READY'
               AND ready.sequence=failed.sequence+1
        )
        BEGIN SELECT RAISE(ABORT, 'mission retry settlement scope is invalid'); END;
    """),
    (68, """
        CREATE TABLE autonomous_epoch_handoff_requests(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            command_id TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL CHECK(action IN (
                'RESTART_FROM_CHECKPOINT','APPLY_BACKLOG_REVISION'
            )),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            expected_mission_version INTEGER NOT NULL
                CHECK(expected_mission_version>0),
            expected_fencing_token INTEGER NOT NULL
                CHECK(expected_fencing_token>0),
            expected_backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            expected_execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            expected_child_job_id INTEGER REFERENCES autonomous_child_jobs(id),
            selected_checkpoint_id INTEGER NOT NULL
                REFERENCES autonomous_mission_checkpoints(id),
            selected_backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            backlog_approval_id INTEGER
                REFERENCES autonomous_backlog_approvals(id),
            revision_authority_id INTEGER
                REFERENCES autonomous_backlog_revision_authorities(id),
            epoch_branch TEXT NOT NULL,
            authentication_context_json TEXT NOT NULL,
            authentication_context_digest TEXT NOT NULL
                CHECK(length(authentication_context_digest)=64
                      AND authentication_context_digest NOT GLOB '*[^0-9a-f]*'),
            request_digest TEXT NOT NULL
                CHECK(length(request_digest)=64
                      AND request_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK((backlog_approval_id IS NULL)<>
                  (revision_authority_id IS NULL))
        );
        CREATE INDEX idx_autonomous_epoch_handoff_requests_mission
            ON autonomous_epoch_handoff_requests(mission_id,id);

        CREATE TABLE autonomous_epoch_handoff_preparations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            request_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_epoch_handoff_requests(id),
            stop_control_command_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_control_commands(id),
            source_mission_version INTEGER NOT NULL CHECK(source_mission_version>0),
            source_fencing_token INTEGER NOT NULL CHECK(source_fencing_token>0),
            stopped_mission_version INTEGER NOT NULL CHECK(stopped_mission_version>0),
            stopped_fencing_token INTEGER NOT NULL CHECK(stopped_fencing_token>0),
            child_job_id INTEGER REFERENCES autonomous_child_jobs(id),
            preparation_digest TEXT NOT NULL
                CHECK(length(preparation_digest)=64
                      AND preparation_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE autonomous_epoch_handoff_results(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            request_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_epoch_handoff_requests(id),
            preparation_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_epoch_handoff_preparations(id),
            source_execution_epoch_id INTEGER NOT NULL
                REFERENCES autonomous_mission_execution_epochs(id),
            result_execution_epoch_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_execution_epochs(id),
            selected_checkpoint_id INTEGER NOT NULL
                REFERENCES autonomous_mission_checkpoints(id),
            selected_backlog_revision_id INTEGER NOT NULL
                REFERENCES autonomous_backlog_revisions(id),
            execution_authorization_id INTEGER NOT NULL
                REFERENCES autonomous_local_authorizations(id),
            result_mission_version INTEGER NOT NULL CHECK(result_mission_version>0),
            result_fencing_token INTEGER NOT NULL CHECK(result_fencing_token>0),
            result_digest TEXT NOT NULL
                CHECK(length(result_digest)=64
                      AND result_digest NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_autonomous_epoch_handoff_results_epoch
            ON autonomous_epoch_handoff_results(result_execution_epoch_id);

        CREATE TRIGGER autonomous_epoch_handoff_requests_no_update
        BEFORE UPDATE ON autonomous_epoch_handoff_requests
        BEGIN SELECT RAISE(ABORT, 'epoch handoff requests are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_requests_no_delete
        BEFORE DELETE ON autonomous_epoch_handoff_requests
        BEGIN SELECT RAISE(ABORT, 'epoch handoff requests are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_request_scope_valid
        BEFORE INSERT ON autonomous_epoch_handoff_requests
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_mission_control_fences fence
                ON fence.mission_id=mission.id
              JOIN autonomous_mission_checkpoints checkpoint
                ON checkpoint.id=NEW.selected_checkpoint_id
              JOIN autonomous_backlog_revisions revision
                ON revision.id=NEW.selected_backlog_revision_id
             WHERE mission.id=NEW.mission_id
               AND mission.mission_owner=NEW.actor
               AND mission.version=NEW.expected_mission_version
               AND mission.disposition='RUNNING'
               AND mission.active_backlog_revision_id=
                   NEW.expected_backlog_revision_id
               AND mission.active_backlog_revision_id=
                   NEW.selected_backlog_revision_id
               AND mission.active_execution_epoch_id=
                   NEW.expected_execution_epoch_id
               AND fence.fencing_token=NEW.expected_fencing_token
               AND fence.disposition='RUNNING'
               AND checkpoint.mission_id=NEW.mission_id
               AND revision.mission_id=NEW.mission_id
               AND (
                   (NEW.expected_child_job_id IS NULL AND NOT EXISTS (
                       SELECT 1 FROM autonomous_child_jobs job
                       LEFT JOIN autonomous_child_reconciliations reconciliation
                         ON reconciliation.child_job_id=job.id
                       LEFT JOIN autonomous_mission_retry_requests retry
                         ON retry.child_job_id=job.id
                       WHERE job.mission_id=NEW.mission_id
                         AND job.backlog_revision_id=
                             NEW.expected_backlog_revision_id
                         AND job.execution_epoch_id=
                             NEW.expected_execution_epoch_id
                         AND reconciliation.id IS NULL AND retry.id IS NULL
                   )) OR
                   (NEW.expected_child_job_id IS NOT NULL AND EXISTS (
                       SELECT 1 FROM autonomous_child_jobs job
                       LEFT JOIN autonomous_child_reconciliations reconciliation
                         ON reconciliation.child_job_id=job.id
                       LEFT JOIN autonomous_mission_retry_requests retry
                         ON retry.child_job_id=job.id
                       WHERE job.id=NEW.expected_child_job_id
                         AND job.mission_id=NEW.mission_id
                         AND job.backlog_revision_id=
                             NEW.expected_backlog_revision_id
                         AND job.execution_epoch_id=
                             NEW.expected_execution_epoch_id
                         AND reconciliation.id IS NULL AND retry.id IS NULL
                   ))
               )
               AND (
                   (NEW.backlog_approval_id IS NOT NULL AND EXISTS (
                       SELECT 1
                         FROM autonomous_backlog_approvals approval
                         JOIN autonomous_backlog_approval_completions completion
                           ON completion.approval_id=approval.id
                        WHERE approval.id=NEW.backlog_approval_id
                          AND approval.mission_id=NEW.mission_id
                          AND approval.revision_id=
                              NEW.selected_backlog_revision_id
                   )) OR
                   (NEW.revision_authority_id IS NOT NULL AND EXISTS (
                       SELECT 1
                         FROM autonomous_backlog_revision_authorities authority
                        WHERE authority.id=NEW.revision_authority_id
                          AND authority.mission_id=NEW.mission_id
                          AND authority.revision_id=
                              NEW.selected_backlog_revision_id
                          AND authority.outcome='APPLIED'
                   ))
               )
        )
        BEGIN SELECT RAISE(ABORT, 'epoch handoff request scope is invalid'); END;

        CREATE TRIGGER autonomous_epoch_handoff_preparations_no_update
        BEFORE UPDATE ON autonomous_epoch_handoff_preparations
        BEGIN SELECT RAISE(ABORT, 'epoch handoff preparations are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_preparations_no_delete
        BEFORE DELETE ON autonomous_epoch_handoff_preparations
        BEGIN SELECT RAISE(ABORT, 'epoch handoff preparations are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_preparation_scope_valid
        BEFORE INSERT ON autonomous_epoch_handoff_preparations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_epoch_handoff_requests request
              JOIN autonomous_mission_control_commands control
                ON control.id=NEW.stop_control_command_id
             WHERE request.id=NEW.request_id
               AND control.mission_id=request.mission_id
               AND control.action='STOP'
               AND control.command_id=request.command_id || ':safe-boundary'
               AND control.expected_mission_version=
                   NEW.source_mission_version
               AND control.expected_fencing_token=NEW.source_fencing_token
               AND control.result_mission_version=
                   NEW.stopped_mission_version
               AND control.result_fencing_token=NEW.stopped_fencing_token
               AND COALESCE(control.child_job_id,-1)=
                   COALESCE(NEW.child_job_id,-1)
        )
        BEGIN SELECT RAISE(ABORT, 'epoch handoff preparation scope is invalid'); END;

        CREATE TRIGGER autonomous_epoch_handoff_results_no_update
        BEFORE UPDATE ON autonomous_epoch_handoff_results
        BEGIN SELECT RAISE(ABORT, 'epoch handoff results are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_results_no_delete
        BEFORE DELETE ON autonomous_epoch_handoff_results
        BEGIN SELECT RAISE(ABORT, 'epoch handoff results are immutable'); END;
        CREATE TRIGGER autonomous_epoch_handoff_result_scope_valid
        BEFORE INSERT ON autonomous_epoch_handoff_results
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_epoch_handoff_requests request
              JOIN autonomous_epoch_handoff_preparations preparation
                ON preparation.id=NEW.preparation_id
              JOIN autonomous_mission_execution_epochs epoch
                ON epoch.id=NEW.result_execution_epoch_id
              JOIN autonomous_missions mission
                ON mission.id=request.mission_id
              JOIN autonomous_mission_control_fences fence
                ON fence.mission_id=mission.id
              JOIN autonomous_local_authorizations authorization
                ON authorization.id=NEW.execution_authorization_id
             WHERE request.id=NEW.request_id
               AND preparation.request_id=request.id
               AND NEW.source_execution_epoch_id=
                   request.expected_execution_epoch_id
               AND NEW.selected_checkpoint_id=request.selected_checkpoint_id
               AND NEW.selected_backlog_revision_id=
                   request.selected_backlog_revision_id
               AND epoch.mission_id=request.mission_id
               AND epoch.supersedes_epoch_id=
                   request.expected_execution_epoch_id
               AND epoch.base_checkpoint_id=request.selected_checkpoint_id
               AND epoch.base_backlog_revision_id=
                   request.selected_backlog_revision_id
               AND epoch.origin=CASE request.action
                   WHEN 'RESTART_FROM_CHECKPOINT' THEN 'CHECKPOINT_RESTART'
                   ELSE 'BACKLOG_REVISION_RESTART' END
               AND authorization.mission_id=request.mission_id
               AND authorization.backlog_revision_id=
                   request.selected_backlog_revision_id
               AND authorization.execution_epoch_id=epoch.id
               AND NOT EXISTS (
                   SELECT 1 FROM autonomous_authorization_revocations revoked
                    WHERE revoked.authorization_id=authorization.id
               )
               AND mission.active_execution_epoch_id=epoch.id
               AND mission.active_backlog_revision_id=
                   request.selected_backlog_revision_id
               AND mission.current_checkpoint_id=request.selected_checkpoint_id
               AND mission.disposition='RUNNING'
               AND mission.version=NEW.result_mission_version
               AND fence.execution_epoch_id=epoch.id
               AND fence.backlog_revision_id=
                   request.selected_backlog_revision_id
               AND fence.disposition='RUNNING'
               AND fence.fencing_token=NEW.result_fencing_token
        )
        BEGIN SELECT RAISE(ABORT, 'epoch handoff result scope is invalid'); END;
    """),
    (69, """
        CREATE TABLE autonomous_mission_temporal_runs(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            workflow_id TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE,
            previous_run_id TEXT,
            first_run_id TEXT NOT NULL,
            mission_version INTEGER NOT NULL CHECK(mission_version>0),
            phase TEXT NOT NULL CHECK(phase IN (
                'DRAFT','SPECIFICATION_ANALYSIS','BACKLOG_GENERATION',
                'WAITING_FOR_BACKLOG_APPROVAL','APPROVED',
                'ENVIRONMENT_DISCOVERY','ENVIRONMENT_BOOTSTRAP','DEVELOPMENT',
                'VALIDATION','INTEGRATION','FINAL_VALIDATION','COMPLETED'
            )),
            disposition TEXT NOT NULL CHECK(disposition IN (
                'RUNNING','PAUSED','STOPPED','NEEDS_ATTENTION',
                'NEEDS_HUMAN_ACTION','REPLANNING','RECOVERING','FAILED'
            )),
            active_backlog_revision_id INTEGER
                REFERENCES autonomous_backlog_revisions(id),
            active_execution_epoch_id INTEGER
                REFERENCES autonomous_mission_execution_epochs(id),
            current_checkpoint_id INTEGER
                REFERENCES autonomous_mission_checkpoints(id),
            control_fencing_token INTEGER NOT NULL CHECK(control_fencing_token>0),
            workflow_build_id TEXT NOT NULL,
            rollover_reason TEXT CHECK(rollover_reason IS NULL OR rollover_reason IN (
                'SAFE_BOUNDARY_THRESHOLD','HISTORY_EVENT_THRESHOLD',
                'TEMPORAL_RECOMMENDATION','WORKER_DEPLOYMENT_CHANGED'
            )),
            previous_run_history_event_count INTEGER NOT NULL DEFAULT 0
                CHECK(previous_run_history_event_count>=0),
            previous_run_safe_boundary_count INTEGER NOT NULL DEFAULT 0
                CHECK(previous_run_safe_boundary_count>=0),
            accepted_mutation_count INTEGER NOT NULL DEFAULT 0
                CHECK(accepted_mutation_count>=0),
            run_digest TEXT NOT NULL
                CHECK(length(run_digest)=64
                      AND run_digest NOT GLOB '*[^0-9a-f]*'),
            registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,sequence),
            UNIQUE(mission_id,workflow_id,run_id),
            CHECK(
                (sequence=1 AND previous_run_id IS NULL
                 AND first_run_id=run_id AND rollover_reason IS NULL) OR
                (sequence>1 AND previous_run_id IS NOT NULL
                 AND rollover_reason IS NOT NULL)
            )
        );
        CREATE INDEX idx_autonomous_mission_temporal_runs_chain
            ON autonomous_mission_temporal_runs(mission_id,sequence);
        CREATE INDEX idx_autonomous_mission_temporal_runs_workflow
            ON autonomous_mission_temporal_runs(workflow_id,sequence);

        CREATE TRIGGER autonomous_mission_temporal_runs_no_update
        BEFORE UPDATE ON autonomous_mission_temporal_runs
        BEGIN SELECT RAISE(ABORT, 'mission Temporal runs are immutable'); END;
        CREATE TRIGGER autonomous_mission_temporal_runs_no_delete
        BEFORE DELETE ON autonomous_mission_temporal_runs
        BEGIN SELECT RAISE(ABORT, 'mission Temporal runs are immutable'); END;
        CREATE TRIGGER autonomous_mission_temporal_run_sequence_valid
        BEFORE INSERT ON autonomous_mission_temporal_runs
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_mission_temporal_runs
             WHERE mission_id=NEW.mission_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission Temporal run sequence must be append-only'); END;
        CREATE TRIGGER autonomous_mission_temporal_run_chain_valid
        BEFORE INSERT ON autonomous_mission_temporal_runs
        WHEN NEW.sequence>1 AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_temporal_runs previous
             WHERE previous.mission_id=NEW.mission_id
               AND previous.sequence=NEW.sequence-1
               AND previous.run_id=NEW.previous_run_id
               AND previous.workflow_id=NEW.workflow_id
               AND previous.first_run_id=NEW.first_run_id
               AND previous.accepted_mutation_count<=NEW.accepted_mutation_count
        )
        BEGIN SELECT RAISE(ABORT, 'mission Temporal run chain is invalid'); END;
        CREATE TRIGGER autonomous_mission_temporal_run_scope_valid
        BEFORE INSERT ON autonomous_mission_temporal_runs
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
              JOIN autonomous_mission_control_fences fence
                ON fence.mission_id=mission.id
             WHERE mission.id=NEW.mission_id
               AND mission.version=NEW.mission_version
               AND mission.phase=NEW.phase
               AND mission.disposition=NEW.disposition
               AND mission.active_backlog_revision_id
                   IS NEW.active_backlog_revision_id
               AND mission.active_execution_epoch_id
                   IS NEW.active_execution_epoch_id
               AND mission.current_checkpoint_id IS NEW.current_checkpoint_id
               AND fence.mission_version=NEW.mission_version
               AND fence.phase=NEW.phase
               AND fence.disposition=NEW.disposition
               AND fence.backlog_revision_id IS NEW.active_backlog_revision_id
               AND fence.execution_epoch_id IS NEW.active_execution_epoch_id
               AND fence.fencing_token=NEW.control_fencing_token
               AND (
                   NEW.active_backlog_revision_id IS NULL OR EXISTS (
                       SELECT 1 FROM autonomous_backlog_revisions revision
                        WHERE revision.id=NEW.active_backlog_revision_id
                          AND revision.mission_id=NEW.mission_id
                   )
               )
               AND (
                   NEW.active_execution_epoch_id IS NULL OR EXISTS (
                       SELECT 1 FROM autonomous_mission_execution_epochs epoch
                        WHERE epoch.id=NEW.active_execution_epoch_id
                          AND epoch.mission_id=NEW.mission_id
                   )
               )
               AND (
                   NEW.current_checkpoint_id IS NULL OR EXISTS (
                       SELECT 1 FROM autonomous_mission_checkpoints checkpoint
                        WHERE checkpoint.id=NEW.current_checkpoint_id
                          AND checkpoint.mission_id=NEW.mission_id
                          AND checkpoint.execution_epoch_id=
                              NEW.active_execution_epoch_id
                   )
               )
        )
        BEGIN SELECT RAISE(ABORT, 'mission Temporal run scope is invalid'); END;
    """),
    (70, """
        DROP TRIGGER workflow_mutation_scope_immutable;
        ALTER TABLE workflow_mutations RENAME TO workflow_mutations_legacy;

        CREATE TABLE workflow_mutations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            run_id INTEGER NOT NULL REFERENCES workflow_runs(id),
            stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
            operation TEXT NOT NULL CHECK(operation IN (
                'provider_call','command','installation','service',
                'model_lifecycle','worktree','git_integration','github',
                'checkpoint','revision_transition','epoch_transition'
            )),
            idempotency_key TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            reconciliation_policy TEXT NOT NULL CHECK(reconciliation_policy IN (
                'verify_then_retry','verify_only','idempotent_replay','manual'
            )),
            status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN (
                'reserved','running','unknown','retry_ready','completed','failed',
                'reconciled','needs_attention'
            )),
            result_json TEXT,
            result_digest TEXT CHECK(
                result_digest IS NULL OR
                (length(result_digest)=64
                 AND result_digest NOT GLOB '*[^0-9a-f]*')
            ),
            evidence_json TEXT NOT NULL DEFAULT '{}',
            evidence_digest TEXT CHECK(
                evidence_digest IS NULL OR
                (length(evidence_digest)=64
                 AND evidence_digest NOT GLOB '*[^0-9a-f]*')
            ),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            UNIQUE(run_id,operation,idempotency_key)
        );
        INSERT INTO workflow_mutations(
            id,identity,run_id,stage_id,operation,idempotency_key,
            request_json,request_digest,reconciliation_policy,status,
            result_json,result_digest,evidence_json,evidence_digest,
            created_at,updated_at,completed_at
        )
        SELECT id,identity,run_id,stage_id,operation,idempotency_key,
               '{}',request_digest,
               CASE operation
                   WHEN 'provider_call' THEN 'idempotent_replay'
                   ELSE 'verify_then_retry'
               END,
               status,result_json,NULL,'{}',
               '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
               created_at,COALESCE(completed_at,created_at),completed_at
          FROM workflow_mutations_legacy;
        DROP TABLE workflow_mutations_legacy;

        CREATE TRIGGER workflow_mutation_scope_immutable
        BEFORE UPDATE ON workflow_mutations
        WHEN NEW.identity<>OLD.identity OR NEW.run_id<>OLD.run_id
          OR NEW.stage_id<>OLD.stage_id OR NEW.operation<>OLD.operation
          OR NEW.idempotency_key<>OLD.idempotency_key
          OR NEW.request_json<>OLD.request_json
          OR NEW.request_digest<>OLD.request_digest
          OR NEW.reconciliation_policy<>OLD.reconciliation_policy
          OR NEW.created_at<>OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'workflow mutation scope is immutable'); END;
        CREATE TRIGGER workflow_mutation_valid_transition
        BEFORE UPDATE OF status ON workflow_mutations
        WHEN NEW.status<>OLD.status AND NOT (
            (OLD.status='reserved' AND NEW.status IN (
                'running','unknown','retry_ready','completed','failed',
                'needs_attention')) OR
            (OLD.status='running' AND NEW.status IN (
                'unknown','completed','failed')) OR
            (OLD.status='unknown' AND NEW.status IN (
                'reconciled','retry_ready','needs_attention')) OR
            (OLD.status='retry_ready' AND NEW.status IN (
                'running','unknown','completed','failed','needs_attention'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid workflow mutation transition'); END;
        CREATE TRIGGER workflow_mutation_result_immutable
        BEFORE UPDATE ON workflow_mutations
        WHEN NEW.status=OLD.status AND (
            COALESCE(NEW.result_json,'')<>COALESCE(OLD.result_json,'') OR
            COALESCE(NEW.result_digest,'')<>COALESCE(OLD.result_digest,'') OR
            NEW.evidence_json<>OLD.evidence_json OR
            COALESCE(NEW.evidence_digest,'')<>COALESCE(OLD.evidence_digest,'') OR
            COALESCE(NEW.completed_at,'')<>COALESCE(OLD.completed_at,'')
        )
        BEGIN SELECT RAISE(ABORT, 'workflow mutation result is immutable'); END;
        CREATE TRIGGER workflow_mutations_no_delete
        BEFORE DELETE ON workflow_mutations
        BEGIN SELECT RAISE(ABORT, 'workflow mutations are durable'); END;

        CREATE TABLE autonomous_mission_operations(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            operation_key TEXT NOT NULL,
            operation_class TEXT NOT NULL CHECK(operation_class IN (
                'provider_call','command','installation','service',
                'model_lifecycle','worktree','git_integration','github',
                'checkpoint','revision_transition','epoch_transition'
            )),
            request_json TEXT NOT NULL,
            request_digest TEXT NOT NULL CHECK(
                length(request_digest)=64
                AND request_digest NOT GLOB '*[^0-9a-f]*'
            ),
            reconciliation_policy TEXT NOT NULL CHECK(reconciliation_policy IN (
                'verify_then_retry','verify_only','idempotent_replay','manual'
            )),
            mission_version INTEGER NOT NULL CHECK(mission_version>0),
            backlog_revision_id INTEGER REFERENCES autonomous_backlog_revisions(id),
            execution_epoch_id INTEGER
                REFERENCES autonomous_mission_execution_epochs(id),
            checkpoint_id INTEGER REFERENCES autonomous_mission_checkpoints(id),
            child_job_id INTEGER REFERENCES autonomous_child_jobs(id),
            stable_item_id TEXT,
            control_fencing_token INTEGER NOT NULL CHECK(control_fencing_token>0),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,operation_key),
            CHECK(child_job_id IS NOT NULL OR stable_item_id IS NULL)
        );
        CREATE INDEX idx_autonomous_mission_operations_scope
            ON autonomous_mission_operations(mission_id,execution_epoch_id,id);

        CREATE TABLE autonomous_mission_operation_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            operation_id INTEGER NOT NULL
                REFERENCES autonomous_mission_operations(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            event_key TEXT NOT NULL,
            lifecycle TEXT NOT NULL CHECK(lifecycle IN (
                'reserved','running','unknown','retry_ready','completed','failed',
                'reconciled','needs_attention'
            )),
            result_json TEXT NOT NULL,
            result_digest TEXT NOT NULL CHECK(
                length(result_digest)=64
                AND result_digest NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_json TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64
                AND evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            decision TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(operation_id,sequence),
            UNIQUE(operation_id,event_key)
        );
        CREATE INDEX idx_autonomous_operation_events_latest
            ON autonomous_mission_operation_events(operation_id,sequence);

        CREATE TRIGGER autonomous_mission_operation_scope_valid
        BEFORE INSERT ON autonomous_mission_operations
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_missions mission
             JOIN autonomous_mission_control_fences fence
                ON fence.mission_id=mission.id
             WHERE mission.id=NEW.mission_id
               AND mission.disposition='RUNNING'
               AND mission.version=NEW.mission_version
               AND mission.active_backlog_revision_id IS NEW.backlog_revision_id
               AND mission.active_execution_epoch_id IS NEW.execution_epoch_id
               AND mission.current_checkpoint_id IS NEW.checkpoint_id
               AND fence.disposition='RUNNING'
               AND fence.fencing_token=NEW.control_fencing_token
               AND (NEW.child_job_id IS NULL OR EXISTS (
                   SELECT 1 FROM autonomous_child_jobs child
                    WHERE child.id=NEW.child_job_id
                      AND child.mission_id=NEW.mission_id
                      AND child.backlog_revision_id=NEW.backlog_revision_id
                      AND child.execution_epoch_id=NEW.execution_epoch_id
                      AND child.stable_item_id=NEW.stable_item_id
               ))
        )
        BEGIN SELECT RAISE(ABORT, 'mission operation scope is stale'); END;
        CREATE TRIGGER autonomous_mission_operations_no_update
        BEFORE UPDATE ON autonomous_mission_operations
        BEGIN SELECT RAISE(ABORT, 'mission operations are immutable'); END;
        CREATE TRIGGER autonomous_mission_operations_no_delete
        BEFORE DELETE ON autonomous_mission_operations
        BEGIN SELECT RAISE(ABORT, 'mission operations are durable'); END;
        CREATE TRIGGER autonomous_operation_event_sequence_valid
        BEFORE INSERT ON autonomous_mission_operation_events
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_mission_operation_events
             WHERE operation_id=NEW.operation_id
        )
        BEGIN SELECT RAISE(ABORT, 'mission operation events must be append-only'); END;
        CREATE TRIGGER autonomous_operation_event_initial_valid
        BEFORE INSERT ON autonomous_mission_operation_events
        WHEN NEW.sequence=1 AND NEW.lifecycle<>'reserved'
        BEGIN SELECT RAISE(ABORT, 'mission operation must begin reserved'); END;
        CREATE TRIGGER autonomous_operation_event_transition_valid
        BEFORE INSERT ON autonomous_mission_operation_events
        WHEN NEW.sequence>1 AND NOT EXISTS (
            SELECT 1 FROM autonomous_mission_operation_events previous
             WHERE previous.operation_id=NEW.operation_id
               AND previous.sequence=NEW.sequence-1
               AND (
                   (previous.lifecycle='reserved' AND NEW.lifecycle IN (
                       'running','unknown','retry_ready','completed','failed',
                       'needs_attention')) OR
                   (previous.lifecycle='running' AND NEW.lifecycle IN (
                       'unknown','completed','failed')) OR
                   (previous.lifecycle='unknown' AND NEW.lifecycle IN (
                       'reconciled','retry_ready','needs_attention')) OR
                   (previous.lifecycle='retry_ready' AND NEW.lifecycle IN (
                       'running','unknown','completed','failed','needs_attention'))
               )
        )
        BEGIN SELECT RAISE(ABORT, 'invalid mission operation lifecycle transition'); END;
        CREATE TRIGGER autonomous_operation_events_no_update
        BEFORE UPDATE ON autonomous_mission_operation_events
        BEGIN SELECT RAISE(ABORT, 'mission operation events are immutable'); END;
        CREATE TRIGGER autonomous_operation_events_no_delete
        BEFORE DELETE ON autonomous_mission_operation_events
        BEGIN SELECT RAISE(ABORT, 'mission operation events are durable'); END;

        CREATE TABLE autonomous_mission_recoveries(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            recovery_key TEXT NOT NULL,
            mission_version INTEGER NOT NULL CHECK(mission_version>0),
            disposition TEXT NOT NULL CHECK(disposition IN (
                'RESUME_SAFE','RECONCILE_REQUIRED','NEEDS_ATTENTION',
                'PAUSED','STOPPED','COMPLETED'
            )),
            replay_safe INTEGER NOT NULL CHECK(replay_safe IN (0,1)),
            snapshot_json TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL CHECK(
                length(snapshot_digest)=64
                AND snapshot_digest NOT GLOB '*[^0-9a-f]*'
            ),
            integrity_json TEXT NOT NULL,
            integrity_digest TEXT NOT NULL CHECK(
                length(integrity_digest)=64
                AND integrity_digest NOT GLOB '*[^0-9a-f]*'
            ),
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mission_id,recovery_key)
        );
        CREATE INDEX idx_autonomous_mission_recoveries_scope
            ON autonomous_mission_recoveries(mission_id,id);

        CREATE TABLE autonomous_mission_recovery_decisions(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            recovery_id INTEGER NOT NULL
                REFERENCES autonomous_mission_recoveries(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            decision_type TEXT NOT NULL CHECK(decision_type IN (
                'STATE_RECONSTRUCTED','CHECKPOINT_VERIFIED',
                'GIT_AUTHORITY_VERIFIED','MODEL_LEASE_RECONSTRUCTED',
                'SERVICES_RECONSTRUCTED','OPERATION_MARKED_UNKNOWN',
                'OPERATION_RETRY_READY','OPERATION_RECONCILED',
                'OPERATION_BLOCKED','INTEGRITY_FAILED',
                'RESUME_ALLOWED','RESUME_BLOCKED'
            )),
            operation_id INTEGER REFERENCES autonomous_mission_operations(id),
            decision_json TEXT NOT NULL,
            decision_digest TEXT NOT NULL CHECK(
                length(decision_digest)=64
                AND decision_digest NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(recovery_id,sequence)
        );
        CREATE TRIGGER autonomous_mission_recovery_scope_valid
        BEFORE INSERT ON autonomous_mission_recoveries
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_missions mission
             WHERE mission.id=NEW.mission_id
               AND mission.version=NEW.mission_version
        )
        BEGIN SELECT RAISE(ABORT, 'mission recovery scope changed concurrently'); END;
        CREATE TRIGGER autonomous_mission_recoveries_no_update
        BEFORE UPDATE ON autonomous_mission_recoveries
        BEGIN SELECT RAISE(ABORT, 'mission recoveries are immutable'); END;
        CREATE TRIGGER autonomous_mission_recoveries_no_delete
        BEFORE DELETE ON autonomous_mission_recoveries
        BEGIN SELECT RAISE(ABORT, 'mission recoveries are durable'); END;
        CREATE TRIGGER autonomous_recovery_decision_sequence_valid
        BEFORE INSERT ON autonomous_mission_recovery_decisions
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_mission_recovery_decisions
             WHERE recovery_id=NEW.recovery_id
        )
        BEGIN SELECT RAISE(ABORT, 'recovery decisions must be append-only'); END;
        CREATE TRIGGER autonomous_recovery_decisions_no_update
        BEFORE UPDATE ON autonomous_mission_recovery_decisions
        BEGIN SELECT RAISE(ABORT, 'recovery decisions are immutable'); END;
        CREATE TRIGGER autonomous_recovery_decisions_no_delete
        BEFORE DELETE ON autonomous_mission_recovery_decisions
        BEGIN SELECT RAISE(ABORT, 'recovery decisions are durable'); END;
    """),
    (71, """
        CREATE TABLE autonomous_epoch_worktrees(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            authority_key TEXT NOT NULL UNIQUE CHECK(
                length(authority_key)=64
                AND authority_key NOT GLOB '*[^0-9a-f]*'
            ),
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            mission_key TEXT NOT NULL,
            execution_epoch_id INTEGER NOT NULL UNIQUE
                REFERENCES autonomous_mission_execution_epochs(id),
            epoch_number INTEGER NOT NULL CHECK(epoch_number>0),
            repository_path TEXT NOT NULL,
            repository_key TEXT NOT NULL,
            base_git_commit_sha TEXT NOT NULL CHECK(
                length(base_git_commit_sha) IN (40,64)
                AND base_git_commit_sha NOT GLOB '*[^0-9a-f]*'
            ),
            base_checkpoint_id INTEGER REFERENCES autonomous_mission_checkpoints(id),
            base_checkpoint_digest TEXT CHECK(
                base_checkpoint_digest IS NULL OR
                (length(base_checkpoint_digest)=64
                 AND base_checkpoint_digest NOT GLOB '*[^0-9a-f]*')
            ),
            epoch_branch TEXT NOT NULL,
            branch_key TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            worktree_path_key TEXT NOT NULL UNIQUE,
            mission_segment TEXT NOT NULL,
            policy_version INTEGER NOT NULL CHECK(policy_version=1),
            reservation_json TEXT NOT NULL,
            reservation_digest TEXT NOT NULL CHECK(
                length(reservation_digest)=64
                AND reservation_digest NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL,
            UNIQUE(repository_key,branch_key),
            CHECK(repository_path<>worktree_path),
            CHECK((base_checkpoint_id IS NULL)=(base_checkpoint_digest IS NULL)),
            CHECK(epoch_branch LIKE 'autonomous/%/epoch-%')
        );
        CREATE INDEX idx_autonomous_epoch_worktrees_repository
            ON autonomous_epoch_worktrees(repository_key,epoch_number,id);

        CREATE TABLE autonomous_epoch_worktree_events(
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL UNIQUE,
            epoch_worktree_id INTEGER NOT NULL
                REFERENCES autonomous_epoch_worktrees(id),
            sequence INTEGER NOT NULL CHECK(sequence>0),
            status TEXT NOT NULL CHECK(status IN (
                'RESERVED','PROVISIONING','READY','DIRTY','MISSING','CONFLICT'
            )),
            observation_json TEXT NOT NULL,
            observation_digest TEXT NOT NULL CHECK(
                length(observation_digest)=64
                AND observation_digest NOT GLOB '*[^0-9a-f]*'
            ),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(epoch_worktree_id,sequence)
        );
        CREATE INDEX idx_autonomous_epoch_worktree_events_latest
            ON autonomous_epoch_worktree_events(epoch_worktree_id,sequence);

        CREATE TRIGGER autonomous_epoch_worktree_scope_valid
        BEFORE INSERT ON autonomous_epoch_worktrees
        WHEN NOT EXISTS (
            SELECT 1
              FROM autonomous_mission_execution_epochs epoch
              JOIN autonomous_missions mission ON mission.id=epoch.mission_id
             WHERE epoch.id=NEW.execution_epoch_id
               AND epoch.mission_id=NEW.mission_id
               AND mission.mission_key=NEW.mission_key
               AND epoch.epoch_number=NEW.epoch_number
               AND epoch.base_git_commit_sha=NEW.base_git_commit_sha
               AND epoch.base_checkpoint_id IS NEW.base_checkpoint_id
               AND epoch.base_checkpoint_digest IS NEW.base_checkpoint_digest
               AND epoch.epoch_branch=NEW.epoch_branch
        )
        BEGIN SELECT RAISE(ABORT, 'epoch worktree authority scope is invalid'); END;
        CREATE TRIGGER autonomous_epoch_worktrees_no_update
        BEFORE UPDATE ON autonomous_epoch_worktrees
        BEGIN SELECT RAISE(ABORT, 'epoch worktree authorities are immutable'); END;
        CREATE TRIGGER autonomous_epoch_worktrees_no_delete
        BEFORE DELETE ON autonomous_epoch_worktrees
        BEGIN SELECT RAISE(ABORT, 'epoch worktree authorities are durable'); END;

        CREATE TRIGGER autonomous_epoch_worktree_event_sequence_valid
        BEFORE INSERT ON autonomous_epoch_worktree_events
        WHEN NEW.sequence<>(
            SELECT COALESCE(MAX(sequence),0)+1
              FROM autonomous_epoch_worktree_events
             WHERE epoch_worktree_id=NEW.epoch_worktree_id
        )
        BEGIN SELECT RAISE(ABORT, 'epoch worktree events must be append-only'); END;
        CREATE TRIGGER autonomous_epoch_worktree_event_initial_valid
        BEFORE INSERT ON autonomous_epoch_worktree_events
        WHEN NEW.sequence=1 AND (
            NEW.status<>'RESERVED' OR NOT EXISTS (
                SELECT 1 FROM autonomous_epoch_worktrees authority
                 WHERE authority.id=NEW.epoch_worktree_id
                   AND authority.reservation_json=NEW.observation_json
                   AND authority.reservation_digest=NEW.observation_digest
            )
        )
        BEGIN SELECT RAISE(ABORT, 'epoch worktree must begin with its reservation'); END;
        CREATE TRIGGER autonomous_epoch_worktree_event_transition_valid
        BEFORE INSERT ON autonomous_epoch_worktree_events
        WHEN NEW.sequence>1 AND NOT EXISTS (
            SELECT 1 FROM autonomous_epoch_worktree_events previous
             WHERE previous.epoch_worktree_id=NEW.epoch_worktree_id
               AND previous.sequence=NEW.sequence-1
               AND (
                   (previous.status='RESERVED' AND NEW.status IN (
                       'PROVISIONING','READY','DIRTY','MISSING','CONFLICT')) OR
                   (previous.status='PROVISIONING' AND NEW.status IN (
                       'READY','DIRTY','MISSING','CONFLICT')) OR
                   (previous.status='READY' AND NEW.status IN (
                       'READY','DIRTY','MISSING','CONFLICT')) OR
                   (previous.status='DIRTY' AND NEW.status IN (
                       'READY','DIRTY','MISSING','CONFLICT')) OR
                   (previous.status='MISSING' AND NEW.status IN (
                       'PROVISIONING','READY','MISSING','CONFLICT')) OR
                   (previous.status='CONFLICT' AND NEW.status IN (
                       'READY','DIRTY','MISSING','CONFLICT'))
               )
        )
        BEGIN SELECT RAISE(ABORT, 'invalid epoch worktree event transition'); END;
        CREATE TRIGGER autonomous_epoch_worktree_events_no_update
        BEFORE UPDATE ON autonomous_epoch_worktree_events
        BEGIN SELECT RAISE(ABORT, 'epoch worktree events are immutable'); END;
        CREATE TRIGGER autonomous_epoch_worktree_events_no_delete
        BEFORE DELETE ON autonomous_epoch_worktree_events
        BEGIN SELECT RAISE(ABORT, 'epoch worktree events are durable'); END;
    """),
    (72, """
        CREATE TABLE autonomous_child_stage_assignments(
            id INTEGER PRIMARY KEY,
            child_job_id INTEGER NOT NULL REFERENCES autonomous_child_jobs(id),
            stage_key TEXT NOT NULL CHECK(length(stage_key) BETWEEN 1 AND 200),
            decision_id INTEGER NOT NULL REFERENCES autonomous_authorization_decisions(id),
            agent_json TEXT NOT NULL CHECK(json_valid(agent_json)),
            effective_model TEXT NOT NULL CHECK(length(effective_model)>0),
            binding_digest TEXT NOT NULL CHECK(length(binding_digest)=64),
            created_at TEXT NOT NULL,
            UNIQUE(child_job_id,stage_key)
        );
        CREATE TRIGGER autonomous_child_stage_assignment_scope_valid
        BEFORE INSERT ON autonomous_child_stage_assignments
        WHEN NOT EXISTS (
            SELECT 1 FROM autonomous_child_jobs job
            JOIN autonomous_authorization_decisions decision ON decision.id=NEW.decision_id
              AND decision.mission_id=job.mission_id
              AND decision.autonomous_authorization_id=job.authorization_id
              AND decision.outcome='ALLOW_AUTONOMOUS' AND decision.authority_valid=1
            JOIN autonomous_child_job_authorizations child ON child.child_job_id=job.id
            WHERE job.id=NEW.child_job_id AND job.execution_mode='live'
              AND json_extract(decision.request_json,'$.task_id')=job.task_id
              AND json_extract(decision.request_json,'$.agent_id')=json_extract(NEW.agent_json,'$.id')
              AND json_extract(decision.request_json,'$.role')=json_extract(NEW.agent_json,'$.role')
              AND json_extract(decision.request_json,'$.model')=json_extract(NEW.agent_json,'$.model')
              AND json_extract(decision.request_json,'$.provider_id')=json_extract(NEW.agent_json,'$.provider')
              AND json_extract(decision.request_json,'$.permissions')=json_extract(NEW.agent_json,'$.permissions')
        )
        BEGIN SELECT RAISE(ABORT, 'autonomous stage assignment scope is invalid'); END;
        CREATE TRIGGER autonomous_child_stage_assignments_no_update
        BEFORE UPDATE ON autonomous_child_stage_assignments
        BEGIN SELECT RAISE(ABORT, 'autonomous stage assignments are immutable'); END;
        CREATE TRIGGER autonomous_child_stage_assignments_no_delete
        BEFORE DELETE ON autonomous_child_stage_assignments
        BEGIN SELECT RAISE(ABORT, 'autonomous stage assignments are durable'); END;
    """),

    (73, """
        CREATE TABLE environment_readiness_reports(
            id INTEGER PRIMARY KEY,
            approval_id INTEGER NOT NULL REFERENCES autonomous_backlog_approvals(id),
            report_json TEXT NOT NULL CHECK(json_valid(report_json)),
            report_digest TEXT NOT NULL CHECK(length(report_digest)=64),
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER environment_readiness_reports_no_update
        BEFORE UPDATE ON environment_readiness_reports
        BEGIN SELECT RAISE(ABORT, 'environment readiness reports are immutable'); END;
        CREATE TRIGGER environment_readiness_reports_no_delete
        BEFORE DELETE ON environment_readiness_reports
        BEGIN SELECT RAISE(ABORT, 'environment readiness reports are durable'); END;
    """),
    (74, """
        CREATE TABLE local_game_drafts(
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, title TEXT NOT NULL,
            idea TEXT NOT NULL, model_key TEXT NOT NULL, view_step INTEGER NOT NULL,
            revision INTEGER NOT NULL, error_code TEXT NOT NULL,
            creation_json TEXT, mission_id INTEGER REFERENCES autonomous_missions(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX local_game_drafts_actor ON local_game_drafts(actor,updated_at,id);
        CREATE TABLE local_game_draft_versions(
            draft_id TEXT NOT NULL REFERENCES local_game_drafts(id),
            revision INTEGER NOT NULL, document_json TEXT NOT NULL CHECK(json_valid(document_json)),
            PRIMARY KEY(draft_id,revision)
        );
        CREATE TRIGGER local_game_draft_versions_no_update BEFORE UPDATE ON local_game_draft_versions
        BEGIN SELECT RAISE(ABORT,'local start history is immutable'); END;
        CREATE TRIGGER local_game_draft_versions_no_delete BEFORE DELETE ON local_game_draft_versions
        BEGIN SELECT RAISE(ABORT,'local start history is durable'); END;
        CREATE TABLE local_game_commands(
            actor TEXT NOT NULL, command_id TEXT NOT NULL, request_digest TEXT NOT NULL,
            draft_id TEXT NOT NULL REFERENCES local_game_drafts(id),
            PRIMARY KEY(actor,command_id)
        );
    """),
    (75, ADMISSION_MIGRATION),
    (76, """
        CREATE TABLE installation_review_plans(
            id INTEGER PRIMARY KEY,
            mission_id INTEGER NOT NULL REFERENCES autonomous_missions(id),
            actor TEXT NOT NULL, command_id TEXT NOT NULL,
            request_digest TEXT NOT NULL, plan_digest TEXT NOT NULL,
            plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            UNIQUE(actor,command_id)
        );
        CREATE INDEX installation_review_mission ON installation_review_plans(mission_id,id);
        CREATE TABLE installation_review_decisions(
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL UNIQUE REFERENCES installation_review_plans(id),
            actor TEXT NOT NULL, command_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            created_at TEXT NOT NULL,
            UNIQUE(actor,command_id)
        );
        CREATE TRIGGER installation_decision_scope BEFORE INSERT ON installation_review_decisions
        WHEN NOT EXISTS (SELECT 1 FROM installation_review_plans p
                         WHERE p.id=NEW.plan_id AND p.actor=NEW.actor AND p.plan_digest=NEW.plan_digest)
        BEGIN SELECT RAISE(ABORT,'installation decision scope differs'); END;
        CREATE TRIGGER installation_plans_no_update BEFORE UPDATE ON installation_review_plans
        BEGIN SELECT RAISE(ABORT,'installation plans are immutable'); END;
        CREATE TRIGGER installation_plans_no_delete BEFORE DELETE ON installation_review_plans
        BEGIN SELECT RAISE(ABORT,'installation plans are durable'); END;
        CREATE TRIGGER installation_decisions_no_update BEFORE UPDATE ON installation_review_decisions
        BEGIN SELECT RAISE(ABORT,'installation decisions are immutable'); END;
        CREATE TRIGGER installation_decisions_no_delete BEFORE DELETE ON installation_review_decisions
        BEGIN SELECT RAISE(ABORT,'installation decisions are durable'); END;
    """),
    (77, """
        -- Lifecycle extension of the existing reservation authority. No old
        -- reservation or usage is rewritten, inferred complete, or refunded.
        CREATE TABLE execution_reservation_closures(
            reservation_id INTEGER PRIMARY KEY REFERENCES execution_stage_reservations(id),
            state TEXT NOT NULL CHECK(state IN ('settled','released')),
            reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TRIGGER execution_closure_scope BEFORE INSERT ON execution_reservation_closures
        WHEN NOT EXISTS (
            SELECT 1 FROM execution_stage_reservations r JOIN execution_traces t ON t.id=r.trace_id
            WHERE r.id=NEW.reservation_id AND r.decision='allowed' AND t.status IN ('active','paused')
              AND ((NEW.state='settled' AND EXISTS (SELECT 1 FROM execution_usage_samples s
                     WHERE s.trace_id=r.trace_id AND s.stage_key=r.stage_key))
                OR (NEW.state='released' AND NOT EXISTS (SELECT 1 FROM execution_usage_samples s
                     WHERE s.trace_id=r.trace_id AND s.stage_key=r.stage_key)))
        )
        BEGIN SELECT RAISE(ABORT,'invalid execution reservation closure'); END;
        CREATE TRIGGER execution_closures_no_update BEFORE UPDATE ON execution_reservation_closures
        BEGIN SELECT RAISE(ABORT,'execution reservation closure is immutable'); END;
        CREATE TRIGGER execution_closures_no_delete BEFORE DELETE ON execution_reservation_closures
        BEGIN SELECT RAISE(ABORT,'execution reservation closure is immutable'); END;
        CREATE TRIGGER execution_usage_after_closure BEFORE INSERT ON execution_usage_samples
        WHEN EXISTS (SELECT 1 FROM execution_stage_reservations r
                     JOIN execution_reservation_closures c ON c.reservation_id=r.id
                     WHERE r.trace_id=NEW.trace_id AND r.stage_key=NEW.stage_key)
        BEGIN SELECT RAISE(ABORT,'execution reservation is closed'); END;
        CREATE INDEX execution_usage_stage ON execution_usage_samples(trace_id,stage_key);
    """),
    (78, PLAYABLE_VERSION_MIGRATION),
    (79, SETTINGS_MIGRATION),
    (80, FEEDBACK_MIGRATION),
    (81, AUTONOMY_MIGRATION),
    (82, SLICE_MIGRATION),
    (83, CYCLE_MIGRATION),
    (84, COST_MIGRATION),
    (85, PAID_TOOL_MIGRATION),
    (86, ROSTER_MIGRATION),
    (87, WORKER_MIGRATION),
    (88, FIRST_RUN_MIGRATION),
    (89, SUPERVISOR_MIGRATION),
)

RUN_TRANSITIONS = TRANSITIONS["run"]

WORKFLOW_MUTATION_OPERATIONS = frozenset(
    {
        "provider_call",
        "command",
        "installation",
        "service",
        "model_lifecycle",
        "worktree",
        "git_integration",
        "github",
        "checkpoint",
        "revision_transition",
        "epoch_transition",
    }
)
WORKFLOW_MUTATION_RECONCILIATION_POLICIES = frozenset(
    {"verify_then_retry", "verify_only", "idempotent_replay", "manual"}
)
WORKFLOW_MUTATION_LIFECYCLES = frozenset(
    {
        "reserved",
        "running",
        "unknown",
        "retry_ready",
        "completed",
        "failed",
        "reconciled",
        "needs_attention",
    }
)


class ScopedApprovalExpiredError(PermissionError):
    """An expiry transition was recorded; never an execution grant."""


class TaskNotRunnableError(RuntimeError):
    """Raised when a work item cannot be dispatched by the scheduler."""


class ConflictDomainBusyError(RuntimeError):
    """Raised when an active assignment owns an overlapping mutation domain."""

    def __init__(self, assignment_ids: tuple[int, ...], *, escalated: bool = False):
        self.assignment_ids = assignment_ids
        self.escalated = escalated
        action = "escalated" if escalated else "serialized"
        super().__init__(
            f"Conflict domains overlap active assignments {list(assignment_ids)}; "
            f"request was {action}"
        )


class StaleLeaseError(PermissionError):
    """Raised when a worker presents an expired or superseded fencing token."""


def _sha256_snapshot(value: str, field: str) -> str:
    """Validate a caller-computed canonical SHA-256 approval snapshot."""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Scheduler timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _normalize_conflict_domains(
    project_id: int, domains: list[str] | tuple[str, ...] | None
) -> tuple[str, ...]:
    requested = domains or ()
    if not requested:
        return (f"project:{project_id}",)
    normalized: set[str] = set()
    prefix = f"project:{project_id}"
    for value in requested:
        domain = str(value).strip().replace("\\", "/").casefold()
        while "//" in domain:
            domain = domain.replace("//", "/")
        domain = domain.rstrip("/")
        if not domain:
            raise ValueError("Conflict domains cannot be empty")
        if domain.startswith("project:"):
            if domain != prefix and not domain.startswith(f"{prefix}/"):
                raise ValueError("Conflict domains cannot cross project boundaries")
        else:
            if domain.startswith("module:"):
                namespace, resource = domain.split(":", 1)
                domain = f"{namespace}:{resource.replace('.', '/')}"
            domain = f"{prefix}/{domain}"
        normalized.add(domain)
    return tuple(sorted(normalized))


def _domains_overlap(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


class SQLiteStorage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._ensure_audit_chain()
        self._ensure_evidence_ledger()

    def _migrate(self) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        applied = {int(row[0]) for row in self.db.execute("SELECT version FROM schema_migrations")}
        for version, script in MIGRATIONS:
            if version in applied:
                continue
            # executescript commits implicitly, so each migration owns its explicit transaction.
            self.db.executescript(f"BEGIN IMMEDIATE;\n{script}\nINSERT INTO schema_migrations(version) VALUES({version});\nCOMMIT;")

    @staticmethod
    def _audit_digest(
        *,
        identity: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: str,
        correlation_json: str,
        created_at: str,
        previous_hash: str,
    ) -> str:
        material = json.dumps(
            {
                "identity": identity,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": json.loads(payload),
                "correlation": json.loads(correlation_json),
                "created_at": created_at,
                "previous_hash": previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _correlation(
        self, entity: str, entity_id: int | str, payload: dict[str, Any]
    ) -> dict[str, str | int | None]:
        correlation: dict[str, str | int | None] = {
            "mission_id": payload.get("mission_id") or payload.get("project_id"),
            "task_id": payload.get("task_id"),
            "run_id": payload.get("run_id"),
            "stage_id": payload.get("stage_id") or payload.get("stage"),
            "attempt_id": payload.get("attempt_id"),
            "worker_session_id": payload.get("worker_session_id"),
        }
        numeric_id = int(entity_id) if str(entity_id).isdigit() else None
        if entity == "project":
            correlation["mission_id"] = entity_id
        elif entity == "task":
            correlation["task_id"] = entity_id
        elif entity == "run":
            correlation["run_id"] = entity_id
        elif entity == "stage":
            correlation["stage_id"] = entity_id
        elif entity == "provider_attempt":
            correlation["attempt_id"] = entity_id

        run_id = correlation["run_id"]
        task_id = correlation["task_id"]
        if numeric_id is not None and entity == "approval":
            row = self.db.execute(
                "SELECT run_id FROM approval_gates WHERE id=?", (numeric_id,)
            ).fetchone()
            run_id = int(row["run_id"]) if row else run_id
        elif numeric_id is not None and entity == "artifact":
            row = self.db.execute(
                "SELECT run_id,stage FROM artifacts WHERE id=?", (numeric_id,)
            ).fetchone()
            if row:
                run_id = int(row["run_id"])
                correlation["stage_id"] = correlation["stage_id"] or row["stage"]
        elif numeric_id is not None and entity == "review_assignment":
            row = self.db.execute(
                "SELECT run_id,stage FROM reviewer_assignments WHERE id=?", (numeric_id,)
            ).fetchone()
            if row:
                run_id = int(row["run_id"])
                correlation["stage_id"] = correlation["stage_id"] or row["stage"]
        elif numeric_id is not None and entity == "provider_gate":
            row = self.db.execute(
                "SELECT task_id FROM provider_execution_gates WHERE id=?", (numeric_id,)
            ).fetchone()
            task_id = int(row["task_id"]) if row else task_id
        elif numeric_id is not None and entity == "provider_attempt":
            row = self.db.execute(
                "SELECT task_id FROM provider_execution_attempts WHERE id=?", (numeric_id,)
            ).fetchone()
            if row:
                task_id = int(row["task_id"])
                session = self.db.execute(
                    """SELECT s.identity
                         FROM attempts a
                         JOIN worker_sessions s ON s.assignment_id=a.assignment_id
                        WHERE a.provider_attempt_id=?""",
                    (numeric_id,),
                ).fetchone()
                if session:
                    correlation["worker_session_id"] = session["identity"]

        if run_id is not None:
            correlation["run_id"] = run_id
            row = self.db.execute(
                "SELECT task_id FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            task_id = int(row["task_id"]) if row else task_id
        if task_id is not None:
            correlation["task_id"] = task_id
            row = self.db.execute(
                "SELECT project_id FROM work_items WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                correlation["mission_id"] = int(row["project_id"])
        return correlation

    def _ensure_audit_chain(self) -> None:
        """One-time compatibility backfill followed by immutable audit guards."""

        previous_hash = ""
        with self.db:
            self._begin_immediate()
            rows = self.db.execute("SELECT * FROM events ORDER BY id").fetchall()
            for row in rows:
                if row["identity"] and row["record_hash"] and row["correlation_json"]:
                    previous_hash = str(row["record_hash"])
                    continue
                decoded_payload = json.loads(row["payload"])
                payload = decoded_payload if isinstance(decoded_payload, dict) else {}
                correlation = self._correlation(
                    str(row["entity_type"]), str(row["entity_id"]), payload
                )
                correlation_json = json.dumps(
                    correlation, sort_keys=True, separators=(",", ":")
                )
                identity = f"event:{row['id']}"
                digest = self._audit_digest(
                    identity=identity,
                    event_type=str(row["event_type"]),
                    entity_type=str(row["entity_type"]),
                    entity_id=str(row["entity_id"]),
                    payload=str(row["payload"]),
                    correlation_json=correlation_json,
                    created_at=str(row["created_at"]),
                    previous_hash=previous_hash,
                )
                self.db.execute(
                    """UPDATE events
                          SET identity=?,correlation_root=?,correlation_json=?,
                              previous_hash=?,record_hash=?
                        WHERE id=?""",
                    (
                        identity,
                        self._correlation_root(correlation),
                        correlation_json,
                        previous_hash,
                        digest,
                        row["id"],
                    ),
                )
                self.db.execute(
                    """INSERT OR IGNORE INTO outbox_messages(
                           identity,event_id,topic,payload,correlation_json,
                           delivery_key,status,delivered_at
                       ) VALUES(?,?,?,?,?,?,'delivered',CURRENT_TIMESTAMP)""",
                    (
                        f"outbox:{row['id']}",
                        row["id"],
                        row["event_type"],
                        row["payload"],
                        correlation_json,
                        identity,
                    ),
                )
                previous_hash = digest
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_identity ON events(identity)"
            )
            self.db.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS events_identity_required
                BEFORE INSERT ON events
                WHEN NEW.identity IS NULL OR NEW.correlation_json IS NULL
                  OR NEW.previous_hash IS NULL OR NEW.record_hash IS NULL
                BEGIN SELECT RAISE(ABORT, 'complete audit envelope is required'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'audit records are immutable'); END;
                """
            )

    @staticmethod
    def _correlation_root(correlation: dict[str, str | int | None]) -> str:
        for key in (
            "mission_id",
            "task_id",
            "run_id",
            "stage_id",
            "attempt_id",
            "worker_session_id",
        ):
            value = correlation.get(key)
            if value is not None:
                return f"{key}:{value}"
        return "control-plane"

    def _begin_immediate(self) -> None:
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _content_digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _ensure_evidence_ledger(self) -> None:
        with self.db:
            for row in self.db.execute(
                """SELECT id,run_id,stage,agent_id,provider,content
                     FROM artifacts WHERE digest IS NULL"""
            ).fetchall():
                self.db.execute(
                    """UPDATE artifacts
                          SET digest=?,producer_json=?,verifier_json=?,inputs_json=?,
                              toolchain_json=?
                        WHERE id=?""",
                    (
                        self._content_digest(str(row["content"])),
                        json.dumps(
                            {"agent_id": row["agent_id"], "provider": row["provider"]},
                            sort_keys=True,
                        ),
                        json.dumps({"status": "unverified"}, sort_keys=True),
                        json.dumps(
                            {"run_id": row["run_id"], "stage": row["stage"]},
                            sort_keys=True,
                        ),
                        json.dumps({"provider": row["provider"]}, sort_keys=True),
                        row["id"],
                    ),
                )
            for row in self.db.execute(
                """SELECT id,gate_id,attempt_id,provider,agent_id,content,metadata
                     FROM provider_execution_artifacts WHERE digest IS NULL"""
            ).fetchall():
                metadata = json.loads(row["metadata"])
                self.db.execute(
                    """UPDATE provider_execution_artifacts
                          SET digest=?,producer_json=?,verifier_json=?,inputs_json=?,
                              toolchain_json=?
                        WHERE id=?""",
                    (
                        self._content_digest(str(row["content"])),
                        json.dumps(
                            {"agent_id": row["agent_id"], "provider": row["provider"]},
                            sort_keys=True,
                        ),
                        json.dumps({"status": "unverified"}, sort_keys=True),
                        json.dumps(
                            {"gate_id": row["gate_id"], "attempt_id": row["attempt_id"]},
                            sort_keys=True,
                        ),
                        json.dumps(
                            {"provider": row["provider"], "metadata": metadata},
                            sort_keys=True,
                        ),
                        row["id"],
                    ),
                )

    def _event(self, kind: str, entity: str, entity_id: int | str, payload: dict[str, Any]) -> None:
        self._begin_immediate()
        identity = self._identity("event")
        payload_json = json.dumps(payload, ensure_ascii=False)
        correlation = self._correlation(entity, entity_id, payload)
        correlation_json = json.dumps(
            correlation, sort_keys=True, separators=(",", ":")
        )
        previous = self.db.execute(
            "SELECT record_hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous["record_hash"]) if previous else ""
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        record_hash = self._audit_digest(
            identity=identity,
            event_type=kind,
            entity_type=entity,
            entity_id=str(entity_id),
            payload=payload_json,
            correlation_json=correlation_json,
            created_at=created_at,
            previous_hash=previous_hash,
        )
        cur = self.db.execute(
            """INSERT INTO events(
                   identity,event_type,entity_type,entity_id,payload,created_at,
                   correlation_root,correlation_json,previous_hash,record_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                identity,
                kind,
                entity,
                str(entity_id),
                payload_json,
                created_at,
                self._correlation_root(correlation),
                correlation_json,
                previous_hash,
                record_hash,
            ),
        )
        event_id = int(cur.lastrowid)
        self.db.execute(
            """INSERT INTO outbox_messages(
                   identity,event_id,topic,payload,correlation_json,delivery_key
               ) VALUES(?,?,?,?,?,?)""",
            (
                self._identity("outbox"),
                event_id,
                kind,
                payload_json,
                correlation_json,
                identity,
            ),
        )

    @staticmethod
    def _identity(kind: str) -> str:
        return f"{kind}:{uuid.uuid4().hex}"

    def event(self, kind: str, entity: str, entity_id: int | str, payload: dict[str, Any]) -> None:
        with self.db:
            self._event(kind, entity, entity_id, payload)

    def verify_audit_chain(self) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        expected_previous = ""
        rows = self.db.execute("SELECT * FROM events ORDER BY id").fetchall()
        required_correlation = {
            "mission_id",
            "task_id",
            "run_id",
            "stage_id",
            "attempt_id",
            "worker_session_id",
        }
        for row in rows:
            reasons: list[str] = []
            try:
                correlation = json.loads(row["correlation_json"])
                if set(correlation) != required_correlation:
                    reasons.append("correlation envelope is incomplete")
            except (TypeError, json.JSONDecodeError):
                correlation = {}
                reasons.append("correlation envelope is invalid JSON")
            if row["previous_hash"] != expected_previous:
                reasons.append("previous hash does not match")
            try:
                expected_hash = self._audit_digest(
                    identity=str(row["identity"]),
                    event_type=str(row["event_type"]),
                    entity_type=str(row["entity_type"]),
                    entity_id=str(row["entity_id"]),
                    payload=str(row["payload"]),
                    correlation_json=str(row["correlation_json"]),
                    created_at=str(row["created_at"]),
                    previous_hash=str(row["previous_hash"]),
                )
                if row["record_hash"] != expected_hash:
                    reasons.append("record hash does not match")
            except (TypeError, ValueError, json.JSONDecodeError):
                reasons.append("record cannot be canonicalized")
            if reasons:
                failures.append({"event_id": int(row["id"]), "reasons": reasons})
            expected_previous = str(row["record_hash"])
        return {"ok": not failures, "checked": len(rows), "failures": failures}

    def verify_evidence_ledger(self) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        checked = 0
        for table in ("artifacts", "provider_execution_artifacts"):
            for row in self.db.execute(
                f"SELECT id,content,digest FROM {table} ORDER BY id"
            ).fetchall():
                checked += 1
                expected = self._content_digest(str(row["content"]))
                if row["digest"] != expected:
                    failures.append(
                        {
                            "artifact": f"{table}:{row['id']}",
                            "reason": "content digest does not match",
                        }
                    )
        for row in self.db.execute(
            """SELECT e.id,e.artifact_digest,a.digest
                 FROM criterion_evidence e
                 JOIN artifacts a ON a.id=e.artifact_id
                WHERE e.status='accepted' ORDER BY e.id"""
        ).fetchall():
            if row["artifact_digest"] != row["digest"]:
                failures.append(
                    {
                        "criterion_evidence_id": int(row["id"]),
                        "reason": "accepted evidence digest does not match artifact",
                    }
                )
        return {"ok": not failures, "checked": checked, "failures": failures}

    def outbox_messages(self, status: str | None = None):
        if status is None:
            return self.db.execute("SELECT * FROM outbox_messages ORDER BY id").fetchall()
        if status not in {"pending", "dispatching", "delivered", "failed"}:
            raise ValueError(status)
        return self.db.execute(
            "SELECT * FROM outbox_messages WHERE status=? ORDER BY id", (status,)
        ).fetchall()

    def claim_outbox(self, consumer: str, limit: int = 25):
        if not consumer.strip():
            raise ValueError("Outbox consumer is required")
        if limit < 1 or limit > 100:
            raise ValueError("Outbox claim limit must be between 1 and 100")
        claimed_ids: list[int] = []
        with self.db:
            self._begin_immediate()
            rows = self.db.execute(
                """SELECT id FROM outbox_messages
                    WHERE status IN ('pending','failed')
                      AND available_at<=CURRENT_TIMESTAMP
                    ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in rows:
                claim_token = uuid.uuid4().hex
                updated = self.db.execute(
                    """UPDATE outbox_messages
                          SET status='dispatching',attempts=attempts+1,
                              claimed_by=?,claim_token=?,last_error=NULL
                        WHERE id=? AND status IN ('pending','failed')""",
                    (consumer, claim_token, row["id"]),
                )
                if updated.rowcount == 1:
                    claimed_ids.append(int(row["id"]))
        if not claimed_ids:
            return []
        placeholders = ",".join("?" for _ in claimed_ids)
        return self.db.execute(
            f"SELECT * FROM outbox_messages WHERE id IN ({placeholders}) ORDER BY id",
            claimed_ids,
        ).fetchall()

    def acknowledge_outbox(self, message_id: int, claim_token: str) -> bool:
        with self.db:
            self._begin_immediate()
            row = self.db.execute(
                "SELECT status,claim_token FROM outbox_messages WHERE id=?", (message_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown outbox message: {message_id}")
            if row["status"] == "delivered":
                return False
            if row["status"] != "dispatching" or row["claim_token"] != claim_token:
                raise PermissionError("Outbox claim token is invalid or stale")
            updated = self.db.execute(
                """UPDATE outbox_messages
                      SET status='delivered',delivered_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='dispatching' AND claim_token=?""",
                (message_id, claim_token),
            )
            return updated.rowcount == 1

    def fail_outbox(
        self, message_id: int, claim_token: str, error: str, retry_seconds: int = 0
    ) -> None:
        if retry_seconds < 0 or retry_seconds > 86_400:
            raise ValueError("retry_seconds must be between 0 and 86400")
        with self.db:
            self._begin_immediate()
            updated = self.db.execute(
                """UPDATE outbox_messages
                      SET status='failed',last_error=?,
                          available_at=datetime('now', ?),claim_token=NULL
                    WHERE id=? AND status='dispatching' AND claim_token=?""",
                (error[:2000], f"+{retry_seconds} seconds", message_id, claim_token),
            )
            if updated.rowcount != 1:
                row = self.db.execute(
                    "SELECT id FROM outbox_messages WHERE id=?", (message_id,)
                ).fetchone()
                if not row:
                    raise KeyError(f"Unknown outbox message: {message_id}")
                raise PermissionError("Outbox claim token is invalid or stale")

    @staticmethod
    def _policy_digest(request: dict[str, Any]) -> str:
        from .policy import canonical_policy_request

        normalized = canonical_policy_request(request)
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @contextmanager
    def _policy_transaction(self):
        """A policy unit owns its transaction or a savepoint in the caller's."""
        if not self.db.in_transaction:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                yield
            return
        # Obtain writer exclusion before reading authority. A stale WAL read
        # snapshot cannot upgrade and must fail instead of granting execution.
        self.db.execute("UPDATE policy_state SET id=id WHERE 0")
        self.db.execute("SAVEPOINT policy_unit")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO policy_unit")
            self.db.execute("RELEASE policy_unit")
            raise
        else:
            self.db.execute("RELEASE policy_unit")

    def policy_state(self) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM policy_state WHERE id=1").fetchone()
        return {
            "emergency_stop": bool(row["emergency_stop"]),
            "reason": str(row["reason"]),
            "actor": str(row["actor"]),
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
        }

    def _assert_dispatch_allowed(self) -> None:
        state = self.policy_state()
        if state["emergency_stop"]:
            raise PermissionError(f"Emergency stop is active: {state['reason']}")

    def record_policy_decision(
        self,
        *,
        request: dict[str, Any],
        request_digest: str,
        outcome: str,
        reason: str,
        policy_version: int,
        approval_id: int | None = None,
    ) -> int:
        from .policy import canonical_policy_request

        request = canonical_policy_request(request)
        if outcome not in {"allow", "deny", "require_approval"}:
            raise ValueError(outcome)
        if self._policy_digest(request) != request_digest:
            raise ValueError("Policy request digest does not match canonical request")
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self._policy_transaction():
            cur = self.db.execute(
                """INSERT INTO policy_decisions(
                       identity,request_digest,request_json,outcome,reason,
                       policy_version,approval_id
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    self._identity("policy-decision"),
                    request_digest,
                    request_json,
                    outcome,
                    reason,
                    policy_version,
                    approval_id,
                ),
            )
            decision_id = int(cur.lastrowid)
            self._event(
                f"policy.{outcome}",
                "policy_decision",
                decision_id,
                {
                    **request,
                    "request_digest": request_digest,
                    "policy_version": policy_version,
                    "approval_id": approval_id,
                    "reason": reason,
                },
            )
        return decision_id

    def request_scoped_approval(
        self,
        *,
        request: dict[str, Any],
        requested_by: str,
        ttl_seconds: int = 900,
    ) -> int:
        from .policy import canonical_policy_request

        request = canonical_policy_request(request)
        if not requested_by.strip():
            raise ValueError("Approval requester is required")
        if ttl_seconds < 1 or ttl_seconds > 86_400:
            raise ValueError("Approval TTL must be between 1 and 86400 seconds")
        with self._policy_transaction():
            task = self.db.execute(
                "SELECT project_id FROM work_items WHERE id=?", (request["task_id"],)
            ).fetchone()
            if not task:
                raise KeyError(f"Unknown task: {request['task_id']}")
            if int(task["project_id"]) != int(request["mission_id"]):
                raise ValueError("Approval mission does not own the requested task")
            if request["run_id"] is not None:
                run = self.db.execute(
                    "SELECT task_id FROM workflow_runs WHERE id=?", (request["run_id"],)
                ).fetchone()
                if not run or int(run["task_id"]) != int(request["task_id"]):
                    raise ValueError("Approval run does not belong to the requested task")
            normalized = dict(request)
            normalized["permissions"] = sorted(set(request["permissions"]))
            digest = self._policy_digest(normalized)
            cur = self.db.execute(
                """INSERT INTO scoped_execution_approvals(
                       identity,mission_id,task_id,run_id,stage_id,worker_id,
                       runtime_id,worktree_id,permissions_json,request_digest,
                       requested_by,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?))""",
                (
                    self._identity("execution-approval"),
                    normalized["mission_id"],
                    normalized["task_id"],
                    normalized["run_id"],
                    normalized["stage_id"],
                    normalized["worker_id"],
                    normalized["runtime_id"],
                    normalized["worktree_id"],
                    json.dumps(normalized["permissions"], separators=(",", ":")),
                    digest,
                    requested_by,
                    f"+{ttl_seconds} seconds",
                ),
            )
            approval_id = int(cur.lastrowid)
            self._event(
                "policy.approval.requested",
                "scoped_approval",
                approval_id,
                {**normalized, "request_digest": digest, "requested_by": requested_by},
            )
        return approval_id

    def decide_scoped_approval(
        self, approval_id: int, decision: str, *, actor: str, note: str = ""
    ) -> bool:
        if decision not in {"approved", "rejected"}:
            raise ValueError(decision)
        if not actor.strip():
            raise ValueError("Approval decision actor is required")
        with self._policy_transaction():
            row = self.db.execute(
                "SELECT * FROM scoped_execution_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown scoped approval: {approval_id}")
            if row["status"] == decision:
                return False
            if row["status"] != "pending":
                raise ValueError(f"Scoped approval {approval_id} is already {row['status']}")
            if self.db.execute(
                "SELECT datetime(?)<=CURRENT_TIMESTAMP", (row["expires_at"],)
            ).fetchone()[0]:
                raise ValueError(f"Scoped approval {approval_id} has expired")
            self.db.execute(
                """UPDATE scoped_execution_approvals
                      SET status=?,decided_by=?,decision_note=?,
                          decided_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='pending'""",
                (decision, actor, note, approval_id),
            )
            self._event(
                f"policy.approval.{decision}",
                "scoped_approval",
                approval_id,
                {
                    "mission_id": row["mission_id"],
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "stage_id": row["stage_id"],
                    "worker_id": row["worker_id"],
                    "runtime_id": row["runtime_id"],
                    "worktree_id": row["worktree_id"],
                    "request_digest": row["request_digest"],
                    "actor": actor,
                    "note": note,
                },
            )
        return True

    def consume_scoped_approval(
        self,
        approval_id: int,
        *,
        request: dict[str, Any],
        request_digest: str,
        assignment_id: int | None = None,
        attempt_id: int | None = None,
    ) -> None:
        from .policy import canonical_policy_request

        request = canonical_policy_request(request)
        if self._policy_digest(request) != request_digest:
            raise PermissionError("Current policy request digest is invalid")
        if (assignment_id is None) != (attempt_id is None):
            raise ValueError("Assignment and attempt must be supplied together")
        expired = False
        with self._policy_transaction():
            self._assert_dispatch_allowed()
            row = self.db.execute(
                "SELECT * FROM scoped_execution_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown scoped approval: {approval_id}")
            if row["status"] != "approved":
                raise PermissionError(
                    f"Scoped approval {approval_id} is {row['status']}, expected approved"
                )
            if self.db.execute(
                "SELECT datetime(?)<=CURRENT_TIMESTAMP", (row["expires_at"],)
            ).fetchone()[0]:
                self.db.execute(
                    "UPDATE scoped_execution_approvals SET status='expired' WHERE id=?",
                    (approval_id,),
                )
                self._event(
                    "policy.approval.expired", "scoped_approval", approval_id, {"task_id": row["task_id"]}
                )
                expired = True
            elif row["request_digest"] != request_digest:
                raise PermissionError("Approval scope does not match the current request digest")
            else:
                stage_row = None
                if assignment_id is not None and attempt_id is not None:
                    assignment = self.db.execute(
                        """SELECT a.*,w.project_id
                             FROM assignments a
                             JOIN work_items w ON w.id=a.task_id
                            WHERE a.id=?""",
                        (assignment_id,),
                    ).fetchone()
                    attempt = self.db.execute(
                        "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                    ).fetchone()
                    if (
                        not assignment
                        or str(assignment["status"]) != "active"
                        or int(assignment["task_id"]) != int(request["task_id"])
                        or int(assignment["project_id"]) != int(request["mission_id"])
                        or str(assignment["agent_id"]) != str(request["worker_id"])
                        or str(assignment["runtime"]) != str(request["runtime_id"])
                    ):
                        raise PermissionError(
                            "Approval assignment scope does not match the live execution"
                        )
                    if (
                        not attempt
                        or int(attempt["assignment_id"]) != assignment_id
                        or str(attempt["status"]) not in {"claimed", "running"}
                    ):
                        raise PermissionError(
                            "Approval attempt is not active for its assignment"
                        )
                    if self.db.execute(
                        "SELECT 1 FROM stage_approval_consumptions WHERE attempt_id=?",
                        (attempt_id,),
                    ).fetchone():
                        raise PermissionError(
                            "This logical attempt already consumed a stage approval"
                        )
                    stage_row = self.db.execute(
                        """SELECT s.id,s.status,r.task_id
                             FROM workflow_stages s
                             JOIN workflow_runs r ON r.id=s.run_id
                            WHERE s.run_id=? AND s.stage_key=?""",
                        (request["run_id"], request["stage_id"]),
                    ).fetchone()
                    if (
                        request["run_id"] is None
                        or not stage_row
                        or int(stage_row["task_id"]) != int(request["task_id"])
                        or str(stage_row["status"]) != "waiting_approval"
                    ):
                        raise PermissionError(
                            "Approval stage is not waiting in the requested run"
                        )
                    worktree = self.db.execute(
                        "SELECT * FROM worktrees WHERE id=?",
                        (request["worktree_id"],),
                    ).fetchone()
                    if (
                        not worktree
                        or int(worktree["assignment_id"]) != assignment_id
                        or (
                            worktree["attempt_id"] is not None
                            and int(worktree["attempt_id"]) != attempt_id
                        )
                        or str(worktree["status"]) not in {"ready", "dirty"}
                        or str(worktree["id"]) != str(request["worktree_id"])
                    ):
                        raise PermissionError(
                            "Approval worktree scope does not match the live attempt"
                        )
                self.db.execute(
                    """UPDATE scoped_execution_approvals
                          SET status='consumed',consumed_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status='approved'""",
                    (approval_id,),
                )
                if stage_row is not None:
                    self.db.execute(
                        """INSERT INTO stage_approval_consumptions(
                               identity,approval_id,attempt_id,assignment_id,
                               run_id,stage_id,request_digest
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            self._identity("stage-approval-consumption"),
                            approval_id,
                            attempt_id,
                            assignment_id,
                            request["run_id"],
                            int(stage_row["id"]),
                            request_digest,
                        ),
                    )
                self._event(
                    "policy.approval.consumed",
                    "scoped_approval",
                    approval_id,
                    {**request, "request_digest": request_digest},
                )
        if expired:
            raise ScopedApprovalExpiredError(f"Scoped approval {approval_id} has expired")

    def set_emergency_stop(self, active: bool, *, actor: str, reason: str) -> bool:
        if not actor.strip() or not reason.strip():
            raise ValueError("Emergency stop requires actor and reason")
        with self.db:
            state = self.db.execute("SELECT * FROM policy_state WHERE id=1").fetchone()
            if bool(state["emergency_stop"]) == active:
                return False
            self.db.execute(
                """UPDATE policy_state
                      SET emergency_stop=?,reason=?,actor=?,version=version+1,
                          updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                (int(active), reason, actor),
            )
            cancelled: dict[str, int] = {}
            if active:
                cancelled["worker_sessions"] = self.db.execute(
                    """UPDATE worker_sessions
                          SET status='cancelled',version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE status IN ('starting','running','suspended')"""
                ).rowcount
                cancelled["attempts"] = self.db.execute(
                    """UPDATE attempts
                          SET status='cancelled',version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE status IN ('claimed','running')"""
                ).rowcount
                cancelled["assignments"] = self.db.execute(
                    """UPDATE assignments
                          SET status='cancelled',version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE status IN ('pending','active','suspended')"""
                ).rowcount
                cancelled["leases"] = self.db.execute(
                    """UPDATE leases
                          SET status='revoked',version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE status='active'"""
                ).rowcount
                provider_attempts = self.db.execute(
                    """SELECT id,gate_id FROM provider_execution_attempts
                        WHERE status IN ('claimed','running')"""
                ).fetchall()
                for attempt in provider_attempts:
                    self.db.execute(
                        """UPDATE provider_execution_attempts
                              SET status='abandoned',version=version+1,
                                  result='Cancelled by Control Plane emergency stop',
                                  metadata='{"emergency_stop":true}',
                                  finished_at=CURRENT_TIMESTAMP,
                                  heartbeat_at=CURRENT_TIMESTAMP
                            WHERE id=?""",
                        (attempt["id"],),
                    )
                    self.db.execute(
                        """UPDATE provider_execution_gates
                              SET status='consumed',consumed_at=CURRENT_TIMESTAMP
                            WHERE id=? AND status='claimed'""",
                        (attempt["gate_id"],),
                    )
                cancelled["provider_attempts"] = len(provider_attempts)
                self.db.execute(
                    """UPDATE provider_execution_gates
                          SET status='rejected',decision_note=?,decided_at=CURRENT_TIMESTAMP
                        WHERE status IN ('pending','approved')""",
                    (f"Emergency stop by {actor}: {reason}",),
                )
                self.db.execute("DELETE FROM pending_provider_gate_claims")
                self.db.execute(
                    """UPDATE scoped_execution_approvals
                          SET status='cancelled',decision_note=?,decided_at=CURRENT_TIMESTAMP
                        WHERE status IN ('pending','approved')""",
                    (f"Emergency stop by {actor}: {reason}",),
                )
            self._event(
                "policy.emergency_stop.activated" if active else "policy.emergency_stop.cleared",
                "policy_state",
                1,
                {"active": active, "actor": actor, "reason": reason, "cancelled": cancelled},
            )
        return True

    def record_worker_qualification(
        self,
        *,
        worker_id: str,
        provider_id: str,
        role: str,
        capabilities: list[str],
        dimensions: dict[str, Any],
        evidence: dict[str, Any],
        status: str,
        ttl_seconds: int,
    ) -> int:
        if status not in {"qualified", "failed"}:
            raise ValueError(status)
        required_dimensions = {
            "availability",
            "reliability",
            "quality",
            "safety",
            "performance",
            "cost",
            "freshness",
            "drift",
        }
        if set(dimensions) != required_dimensions:
            raise ValueError("Qualification health dimensions are incomplete")
        evidence_json = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        evidence_digest = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        capabilities_json = json.dumps(sorted(set(capabilities)), separators=(",", ":"))
        with self.db:
            cur = self.db.execute(
                """INSERT INTO worker_qualifications(
                       identity,worker_id,provider_id,role,capabilities_json,
                       dimensions_json,evidence_json,evidence_digest,status,valid_until
                   ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now', ?))""",
                (
                    self._identity("worker-qualification"),
                    worker_id,
                    provider_id,
                    role,
                    capabilities_json,
                    json.dumps(dimensions, sort_keys=True, separators=(",", ":")),
                    evidence_json,
                    evidence_digest,
                    status,
                    f"+{ttl_seconds} seconds",
                ),
            )
            qualification_id = int(cur.lastrowid)
            current = self.db.execute(
                "SELECT state FROM worker_lifecycle WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if not current:
                self.db.execute(
                    """INSERT INTO worker_lifecycle(worker_id,state,reason)
                       VALUES(?,?,?)""",
                    (
                        worker_id,
                        "active" if status == "qualified" else "offline",
                        "qualification passed" if status == "qualified" else "qualification failed",
                    ),
                )
            self._event(
                f"worker.qualification.{status}",
                "worker_qualification",
                qualification_id,
                {
                    "worker_id": worker_id,
                    "provider_id": provider_id,
                    "role": role,
                    "capabilities": sorted(set(capabilities)),
                    "evidence_digest": evidence_digest,
                },
            )
        return qualification_id

    def set_worker_lifecycle(self, worker_id: str, state: str, *, reason: str) -> None:
        allowed = {
            "active": {"draining", "quarantined", "offline"},
            "draining": {"active", "quarantined", "offline"},
            "quarantined": {"active", "offline"},
            "offline": {"active", "quarantined"},
        }
        if state not in allowed:
            raise ValueError(state)
        if not reason.strip():
            raise ValueError("Worker lifecycle reason is required")
        with self.db:
            self._begin_immediate()
            row = self.db.execute(
                "SELECT state FROM worker_lifecycle WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if not row:
                self.db.execute(
                    "INSERT INTO worker_lifecycle(worker_id,state,reason) VALUES(?,?,?)",
                    (worker_id, state, reason),
                )
                previous = None
            else:
                previous = str(row["state"])
                if previous == state:
                    return
                if state not in allowed[previous]:
                    raise ValueError(f"Invalid worker lifecycle transition: {previous} -> {state}")
                self.db.execute(
                    """UPDATE worker_lifecycle
                          SET state=?,reason=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE worker_id=? AND state=?""",
                    (state, reason, worker_id, previous),
                )
            self._event(
                f"worker.{state}",
                "worker",
                worker_id,
                {"worker_id": worker_id, "previous_state": previous, "reason": reason},
            )

    def select_qualified_worker(
        self,
        *,
        role: str,
        required_capabilities: set[str],
        excluded_workers: set[str] | None = None,
    ) -> str | None:
        excluded_workers = excluded_workers or set()
        rows = self.db.execute(
            """SELECT q.*,COALESCE(l.state,'offline') lifecycle_state
                 FROM worker_qualifications q
                 LEFT JOIN worker_lifecycle l ON l.worker_id=q.worker_id
                WHERE q.id=(
                    SELECT MAX(latest.id) FROM worker_qualifications latest
                     WHERE latest.worker_id=q.worker_id
                )
                  AND q.status='qualified' AND q.role=?
                  AND q.valid_until>CURRENT_TIMESTAMP
                  AND COALESCE(l.state,'offline')='active'
                ORDER BY q.worker_id""",
            (role,),
        ).fetchall()
        for row in rows:
            if row["worker_id"] in excluded_workers:
                continue
            capabilities = set(json.loads(row["capabilities_json"]))
            if required_capabilities <= capabilities:
                return str(row["worker_id"])
        return None

    def create_worker_handoff(
        self,
        *,
        source_worker_id: str,
        replacement_worker_id: str,
        task_id: int,
        run_id: int | None,
        stage_id: str,
        attempt_id: str | None,
        context_digest: str,
        evidence: dict[str, Any],
        reason: str,
    ) -> int:
        _sha256_snapshot(context_digest, "context_digest")
        if source_worker_id == replacement_worker_id:
            raise ValueError("Worker handoff requires a distinct replacement")
        with self.db:
            cur = self.db.execute(
                """INSERT INTO worker_handoffs(
                       identity,source_worker_id,replacement_worker_id,task_id,
                       run_id,stage_id,attempt_id,context_digest,evidence_json,reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("worker-handoff"),
                    source_worker_id,
                    replacement_worker_id,
                    task_id,
                    run_id,
                    stage_id,
                    attempt_id,
                    context_digest,
                    json.dumps(evidence, sort_keys=True),
                    reason,
                ),
            )
            handoff_id = int(cur.lastrowid)
            self._event(
                "worker.handoff.created",
                "worker_handoff",
                handoff_id,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "stage_id": stage_id,
                    "attempt_id": attempt_id,
                    "source_worker_id": source_worker_id,
                    "replacement_worker_id": replacement_worker_id,
                    "context_digest": context_digest,
                    "reason": reason,
                },
            )
        return handoff_id

    def create_assignment_attempt(
        self,
        assignment_id: int,
        fencing_token: int,
        *,
        now: datetime | None = None,
    ) -> int:
        from .worker_admission import admission_for_assignment

        self._begin_immediate()
        try:
            if admission_for_assignment(self, assignment_id) is not None:
                raise PermissionError("Admitted assignments already have one bound attempt")
            attempt_id = self._create_assignment_attempt_in_transaction(
                assignment_id, fencing_token, now=now
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return attempt_id

    def _create_assignment_attempt_in_transaction(
        self,
        assignment_id: int,
        fencing_token: int,
        *,
        now: datetime | None = None,
    ) -> int:
        current = _timestamp(_utc(now))
        self._expire_scheduler_leases(current)
        lease = self._assert_fenced_lease(
            assignment_id, fencing_token, current
        )
        ordinal = int(
            self.db.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM attempts WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()[0]
        )
        cursor = self.db.execute(
            """INSERT INTO attempts(
                   identity,assignment_id,ordinal,status,updated_at
               ) VALUES(?,?,?,'claimed',?)""",
            (
                self._identity("attempt"),
                assignment_id,
                ordinal,
                current,
            ),
        )
        attempt_id = int(cursor.lastrowid)
        self._event(
            "attempt.claimed",
            "attempt",
            attempt_id,
            {
                "task_id": int(lease["task_id"]),
                "assignment_id": assignment_id,
                "fencing_token": fencing_token,
                "ordinal": ordinal,
            },
        )
        return attempt_id

    def store_execution_context_package(
        self,
        *,
        task_id: int,
        run_id: int,
        assignment_id: int,
        fencing_token: int,
        digest: str,
        package_json: str,
        byte_count: int,
        token_count: int,
        compacted: bool,
        now: datetime | None = None,
    ) -> int:
        _sha256_snapshot(digest, "context package digest")
        try:
            decoded = json.loads(package_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Context package must be valid JSON") from exc
        canonical = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        if canonical != package_json:
            raise ValueError("Context package JSON must use canonical encoding")
        encoded = package_json.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("Context package digest does not match its content")
        if byte_count != len(encoded) or token_count != (len(encoded) + 3) // 4:
            raise ValueError("Context package size metadata does not match its content")

        self._begin_immediate()
        try:
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            if int(lease["task_id"]) != task_id:
                raise PermissionError("Context package task is not owned by its lease")
            run = self.db.execute(
                "SELECT task_id FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(f"Unknown workflow run: {run_id}")
            if int(run["task_id"]) != task_id:
                raise PermissionError("Context package run belongs to another task")
            existing = self.db.execute(
                "SELECT * FROM execution_context_packages WHERE digest=?",
                (digest,),
            ).fetchone()
            if existing:
                expected = (task_id, run_id, assignment_id, fencing_token, package_json)
                stored = (
                    int(existing["task_id"]),
                    int(existing["run_id"]),
                    int(existing["assignment_id"]),
                    int(existing["fencing_token"]),
                    str(existing["package_json"]),
                )
                if stored != expected:
                    raise ValueError("Context digest is already bound to another scope")
                self.db.commit()
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO execution_context_packages(
                       identity,task_id,run_id,assignment_id,fencing_token,digest,
                       package_json,byte_count,token_count,compacted
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("execution-context"),
                    task_id,
                    run_id,
                    assignment_id,
                    fencing_token,
                    digest,
                    package_json,
                    byte_count,
                    token_count,
                    int(compacted),
                ),
            )
            package_id = int(cursor.lastrowid)
            self._event(
                "context.package.created",
                "execution_context_package",
                package_id,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "assignment_id": assignment_id,
                    "fencing_token": fencing_token,
                    "digest": digest,
                    "byte_count": byte_count,
                    "token_count": token_count,
                    "compacted": bool(compacted),
                },
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return package_id

    def execution_context_package(self, digest: str):
        _sha256_snapshot(digest, "context package digest")
        row = self.db.execute(
            "SELECT * FROM execution_context_packages WHERE digest=?", (digest,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown execution context package: {digest}")
        return row

    def assert_execution_context_scope(
        self,
        digest: str,
        *,
        task_id: int,
        assignment_id: int,
        fencing_token: int,
    ) -> int:
        row = self.execution_context_package(digest)
        if (
            int(row["task_id"]) != task_id
            or int(row["assignment_id"]) != assignment_id
            or int(row["fencing_token"]) != fencing_token
        ):
            raise PermissionError("Execution context package belongs to another dispatch")
        return int(row["id"])

    def record_hermes_acp_session(
        self,
        *,
        worker_session_id: int,
        task_id: int,
        run_id: int,
        stage_key: str,
        attempt_id: int,
        assignment_id: int,
        fencing_token: int,
        agent_role: str,
        worktree_id: int,
        context_digest: str,
        allowed_tools: tuple[str, ...],
        executable: str,
        hermes_version: str,
        protocol_version: int,
        external_session_id: str,
        process_pid: int | None,
        now: datetime | None = None,
    ) -> int:
        if not stage_key.strip() or not agent_role.strip():
            raise ValueError("Hermes stage and agent role are required")
        if not executable.strip() or not hermes_version.strip():
            raise ValueError("Hermes executable and version evidence are required")
        if protocol_version <= 0 or not external_session_id.strip():
            raise ValueError("Hermes protocol and external session identity are required")
        normalized_tools = tuple(sorted({tool.strip() for tool in allowed_tools if tool.strip()}))
        if not normalized_tools:
            raise ValueError("Hermes requires an explicit non-empty tool allowlist")
        self._begin_immediate()
        try:
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            if int(lease["task_id"]) != task_id:
                raise PermissionError("Hermes task is not owned by its lease")
            worker_session = self.db.execute(
                "SELECT * FROM worker_sessions WHERE id=?", (worker_session_id,)
            ).fetchone()
            if not worker_session or int(worker_session["assignment_id"]) != assignment_id:
                raise PermissionError("Hermes worker session belongs to another assignment")
            context = self.execution_context_package(context_digest)
            if (
                int(context["task_id"]) != task_id
                or int(context["run_id"]) != run_id
                or int(context["assignment_id"]) != assignment_id
                or int(context["fencing_token"]) != fencing_token
                or int(worker_session["context_package_id"] or 0) != int(context["id"])
            ):
                raise PermissionError("Hermes context package scope does not match")
            stage = self.db.execute(
                "SELECT id,status FROM workflow_stages WHERE run_id=? AND stage_key=?",
                (run_id, stage_key),
            ).fetchone()
            if not stage:
                raise KeyError(f"Unknown Hermes stage {stage_key} for run {run_id}")
            if str(stage["status"]) not in {"running", "waiting_approval"}:
                raise PermissionError("Hermes requires an active durable workflow stage")
            attempt = self.db.execute(
                "SELECT assignment_id,status FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if (
                not attempt
                or int(attempt["assignment_id"]) != assignment_id
                or str(attempt["status"]) not in {"claimed", "running"}
            ):
                raise PermissionError("Hermes attempt is not active for its assignment")
            worktree = self.db.execute(
                "SELECT * FROM worktrees WHERE id=?", (worktree_id,)
            ).fetchone()
            if (
                not worktree
                or int(worktree["assignment_id"]) != assignment_id
                or int(worktree["fencing_token"] or 0) != fencing_token
                or str(worktree["status"]) not in {"ready", "dirty"}
            ):
                raise PermissionError("Hermes worktree is not owned by its assignment")
            existing = self.db.execute(
                "SELECT * FROM hermes_acp_sessions WHERE worker_session_id=?",
                (worker_session_id,),
            ).fetchone()
            if existing:
                if str(existing["external_session_id"]) != external_session_id:
                    raise ValueError("Worker session already has another Hermes identity")
                self.db.commit()
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO hermes_acp_sessions(
                       identity,worker_session_id,task_id,run_id,stage_id,attempt_id,
                       assignment_id,fencing_token,agent_role,worktree_id,
                       context_package_id,allowed_tools_json,executable,hermes_version,
                       protocol_version,external_session_id,process_pid,status,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?)""",
                (
                    self._identity("hermes-acp-session"),
                    worker_session_id,
                    task_id,
                    run_id,
                    int(stage["id"]),
                    attempt_id,
                    assignment_id,
                    fencing_token,
                    agent_role,
                    worktree_id,
                    int(context["id"]),
                    json.dumps(normalized_tools, separators=(",", ":")),
                    executable,
                    hermes_version,
                    protocol_version,
                    external_session_id,
                    process_pid,
                    current,
                ),
            )
            hermes_session_id = int(cursor.lastrowid)
            self.db.execute(
                """UPDATE attempts SET status='running',version=version+1,updated_at=?
                    WHERE id=? AND status='claimed'""",
                (current, attempt_id),
            )
            self._event(
                "hermes.session.bound",
                "hermes_acp_session",
                hermes_session_id,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "stage_id": stage_key,
                    "attempt_id": attempt_id,
                    "assignment_id": assignment_id,
                    "fencing_token": fencing_token,
                    "worktree_id": worktree_id,
                    "context_digest": context_digest,
                    "external_session_id": external_session_id,
                    "hermes_version": hermes_version,
                    "protocol_version": protocol_version,
                    "allowed_tools": list(normalized_tools),
                },
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return hermes_session_id

    def hermes_acp_session(self, external_session_id: str):
        row = self.db.execute(
            "SELECT * FROM hermes_acp_sessions WHERE external_session_id=?",
            (external_session_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown Hermes ACP session: {external_session_id}")
        return row

    def update_hermes_acp_session(
        self,
        external_session_id: str,
        *,
        status: str,
        process_pid: int | None = None,
    ) -> None:
        if status not in {"running", "suspended", "succeeded", "failed", "cancelled"}:
            raise ValueError(f"Unknown Hermes ACP status: {status}")
        with self.db:
            row = self.hermes_acp_session(external_session_id)
            source = str(row["status"])
            allowed = {
                "running": {"running", "suspended", "succeeded", "failed", "cancelled"},
                "suspended": {"suspended", "running", "failed", "cancelled"},
                "succeeded": {"succeeded"},
                "failed": {"failed"},
                "cancelled": {"cancelled"},
            }
            if status not in allowed[source]:
                raise ValueError(f"Invalid Hermes ACP transition: {source} -> {status}")
            updated = self.db.execute(
                """UPDATE hermes_acp_sessions
                      SET status=?,process_pid=?,version=version+1,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND version=?""",
                (status, process_pid, int(row["id"]), int(row["version"])),
            )
            if updated.rowcount != 1:
                raise ValueError("Hermes ACP session changed concurrently")
            if status in {"succeeded", "failed", "cancelled"}:
                attempt = self.db.execute(
                    "SELECT status FROM attempts WHERE id=?", (int(row["attempt_id"]),)
                ).fetchone()
                if attempt and str(attempt["status"]) not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    ensure_transition("attempt", str(attempt["status"]), status)
                    self.db.execute(
                        """UPDATE attempts
                              SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                            WHERE id=?""",
                        (status, int(row["attempt_id"])),
                    )
            self._event(
                f"hermes.session.{status}",
                "hermes_acp_session",
                int(row["id"]),
                {"external_session_id": external_session_id},
            )

    def record_codex_worker_result(
        self,
        *,
        worker_session_id: int,
        task_id: int,
        run_id: int,
        stage_key: str,
        attempt_id: int,
        assignment_id: int,
        worktree_id: int,
        context_digest: str,
        codex_version: str,
        producer_model: str,
        permission_profile: dict[str, Any],
        invocation: list[str],
        executed_commands: list[dict[str, Any]],
        changed_files: list[str],
        diff_digest: str,
        status: str,
        exit_code: int | None,
        handoff: dict[str, Any],
        evidence_directory: str,
        evidence_digest: str,
    ) -> int:
        if status not in {"succeeded", "failed", "timed_out", "cancelled", "output_limited"}:
            raise ValueError(f"Unknown Codex worker result status: {status}")
        if not producer_model.strip():
            raise ValueError("Codex producer model identity is required")
        _sha256_snapshot(diff_digest, "Codex diff digest")
        _sha256_snapshot(evidence_digest, "Codex evidence digest")
        with self.db:
            session = self.db.execute(
                "SELECT * FROM worker_sessions WHERE id=?", (worker_session_id,)
            ).fetchone()
            stage = self.db.execute(
                "SELECT id FROM workflow_stages WHERE run_id=? AND stage_key=?",
                (run_id, stage_key),
            ).fetchone()
            consumption = self.db.execute(
                """SELECT * FROM stage_approval_consumptions
                    WHERE attempt_id=? AND assignment_id=? AND run_id=?""",
                (attempt_id, assignment_id, run_id),
            ).fetchone()
            worktree = self.managed_worktree(worktree_id)
            context = self.execution_context_package(context_digest)
            if (
                not session
                or int(session["assignment_id"]) != assignment_id
                or str(session["runtime"]) != "codex-cli"
                or int(session["context_package_id"] or 0) != int(context["id"])
            ):
                raise PermissionError("Codex result session scope does not match")
            if not stage or not consumption or int(consumption["stage_id"]) != int(stage["id"]):
                raise PermissionError("Codex result lacks its exact stage approval consumption")
            if (
                int(worktree["assignment_id"]) != assignment_id
                or int(worktree["attempt_id"] or 0) != attempt_id
                or int(context["task_id"]) != task_id
                or int(context["run_id"]) != run_id
            ):
                raise PermissionError("Codex result worktree or context scope does not match")
            existing = self.db.execute(
                "SELECT id FROM codex_worker_results WHERE worker_session_id=?",
                (worker_session_id,),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO codex_worker_results(
                       identity,worker_session_id,approval_consumption_id,task_id,
                       run_id,stage_id,attempt_id,assignment_id,worktree_id,
                       context_package_id,codex_version,producer_model,permission_profile_json,
                       invocation_json,executed_commands_json,changed_files_json,
                       diff_digest,status,exit_code,handoff_json,evidence_directory,
                       evidence_digest
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("codex-worker-result"), worker_session_id,
                    int(consumption["id"]), task_id, run_id, int(stage["id"]),
                    attempt_id, assignment_id, worktree_id, int(context["id"]),
                    codex_version, producer_model.strip(),
                    json.dumps(permission_profile, sort_keys=True),
                    json.dumps(invocation, separators=(",", ":")),
                    json.dumps(executed_commands, sort_keys=True),
                    json.dumps(sorted(set(changed_files)), separators=(",", ":")),
                    diff_digest, status, exit_code, json.dumps(handoff, sort_keys=True),
                    evidence_directory, evidence_digest,
                ),
            )
            result_id = int(cursor.lastrowid)
            self._event(
                f"codex.worker.{status}", "codex_worker_result", result_id,
                {
                    "task_id": task_id, "run_id": run_id, "stage_id": stage_key,
                    "attempt_id": attempt_id, "assignment_id": assignment_id,
                    "worktree_id": worktree_id, "diff_digest": diff_digest,
                    "changed_files": sorted(set(changed_files)),
                    "evidence_digest": evidence_digest,
                },
            )
        return result_id

    def create_runtime_session(
        self,
        *,
        assignment_id: int,
        runtime: str,
        request: dict[str, Any],
        context_digest: str | None = None,
        fencing_token: int | None = None,
    ) -> int:
        from .worker_admission import admission_for_assignment

        self._begin_immediate()
        try:
            if admission_for_assignment(self, assignment_id) is not None:
                raise PermissionError("Admitted assignments require a bound runtime reservation")
            session_id = self._create_runtime_session_in_transaction(
                assignment_id=assignment_id, runtime=runtime, request=request,
                context_digest=context_digest, fencing_token=fencing_token,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return session_id

    def _create_runtime_session_in_transaction(
        self,
        *,
        assignment_id: int,
        runtime: str,
        request: dict[str, Any],
        context_digest: str | None = None,
        fencing_token: int | None = None,
    ) -> int:
        if not runtime.strip():
            raise ValueError("Runtime identity is required")
        if (context_digest is None) != (fencing_token is None):
            raise ValueError("Context digest and fencing token must be supplied together")
        assignment = self.db.execute(
            "SELECT task_id,status FROM assignments WHERE id=?",
            (assignment_id,),
        ).fetchone()
        if not assignment:
            raise KeyError(f"Unknown assignment: {assignment_id}")
        if str(assignment["status"]) != "active":
            raise ValueError("Runtime session requires an active assignment")
        context_package_id = None
        if context_digest is not None and fencing_token is not None:
            current = _timestamp(_utc())
            self._expire_scheduler_leases(current)
            self._assert_fenced_lease(assignment_id, fencing_token, current)
            context_package_id = self.assert_execution_context_scope(
                context_digest,
                task_id=int(assignment["task_id"]),
                assignment_id=assignment_id,
                fencing_token=fencing_token,
            )
        cursor = self.db.execute(
            """INSERT INTO worker_sessions(
                   identity,assignment_id,runtime,status,request_json,
                   context_package_id,context_digest,updated_at
               ) VALUES(?,?,?,'starting',?,?,?,CURRENT_TIMESTAMP)""",
            (
                self._identity("worker-session"),
                assignment_id,
                runtime,
                json.dumps(request, sort_keys=True),
                context_package_id,
                context_digest,
            ),
        )
        session_id = int(cursor.lastrowid)
        self._event(
            "runtime.session.created",
            "worker_session",
            session_id,
            {
                "task_id": int(assignment["task_id"]),
                "assignment_id": assignment_id,
                "runtime": runtime,
                "context_digest": context_digest,
            },
        )
        return session_id

    def runtime_session(self, session_id: int):
        row = self.db.execute(
            "SELECT * FROM worker_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown runtime session: {session_id}")
        return row

    def _assert_admitted_runtime_session(self, session: sqlite3.Row) -> None:
        from .worker_admission import admission_for_assignment

        admitted = admission_for_assignment(self, int(session["assignment_id"]))
        if admitted is None:
            return
        if admitted["runtime_session_id"] != int(session["id"]):
            raise PermissionError("Runtime session does not match its admission reservation")
        self._assert_fenced_lease(
            int(session["assignment_id"]), int(admitted["fencing_token"]), _timestamp(_utc())
        )

    def assert_runtime_session_authority(
        self, session_id: int, *, allowed_states: tuple[str, ...] | None = None
    ) -> None:
        self._begin_immediate()
        try:
            session = self.runtime_session(session_id)
            self._assert_admitted_runtime_session(session)
            if allowed_states is not None and session["status"] not in allowed_states:
                raise PermissionError("Runtime session state denies this operation")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def start_runtime_session(self, session_id: int, external_session_id: str) -> None:
        if not external_session_id.strip():
            raise ValueError("External session identity is required")
        with self.db:
            self._begin_immediate()
            row = self.runtime_session(session_id)
            self._assert_admitted_runtime_session(row)
            if str(row["status"]) != "starting":
                raise ValueError("Only a starting runtime session can bind externally")
            self.db.execute(
                """UPDATE worker_sessions
                      SET external_session_id=?,status='running',version=version+1,
                          updated_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='starting'""",
                (external_session_id, session_id),
            )
            self._event(
                "runtime.session.started",
                "worker_session",
                session_id,
                {"external_session_id": external_session_id},
            )

    def resume_runtime_session(self, session_id: int) -> None:
        with self.db:
            self._begin_immediate()
            row = self.runtime_session(session_id)
            self._assert_admitted_runtime_session(row)
            status = str(row["status"])
            if status == "running":
                return
            if status != "suspended":
                raise ValueError(f"Runtime session cannot resume from {status}")
            self.db.execute(
                """UPDATE worker_sessions
                      SET status='running',version=version+1,
                          updated_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='suspended'""",
                (session_id,),
            )
            self._event(
                "runtime.session.resumed", "worker_session", session_id, {}
            )

    def suspend_runtime_session(self, session_id: int, *, reason: str) -> None:
        with self.db:
            row = self.runtime_session(session_id)
            if str(row["status"]) != "running":
                raise ValueError("Only a running runtime session can be suspended")
            self.db.execute(
                """UPDATE worker_sessions
                      SET status='suspended',version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='running'""",
                (session_id,),
            )
            self._event(
                "runtime.session.suspended",
                "worker_session",
                session_id,
                {"reason": reason},
            )

    def heartbeat_runtime_session(self, session_id: int) -> str:
        heartbeat = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.db:
            self._begin_immediate()
            row = self.runtime_session(session_id)
            self._assert_admitted_runtime_session(row)
            if str(row["status"]) != "running":
                raise ValueError("Only a running runtime session can heartbeat")
            self.db.execute(
                """UPDATE worker_sessions
                      SET heartbeat_at=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='running'""",
                (heartbeat, session_id),
            )
            self._event(
                "runtime.session.heartbeat",
                "worker_session",
                session_id,
                {"heartbeat_at": heartbeat},
            )
        return heartbeat

    def append_runtime_event(
        self,
        session_id: int,
        *,
        kind: str,
        payload: dict[str, Any],
        mutable: bool = False,
    ) -> int:
        allowed = {"status", "message", "tool_call", "artifact", "heartbeat", "error"}
        if kind not in allowed:
            raise ValueError(f"Unknown runtime event kind: {kind}")
        with self.db:
            self._begin_immediate()
            session = self.runtime_session(session_id)
            if mutable or kind in {"artifact", "tool_call"}:
                self._assert_admitted_runtime_session(session)
            if str(session["status"]) not in {"starting", "running", "suspended"}:
                raise ValueError("Cannot append events to a terminal runtime session")
            sequence = int(
                self.db.execute(
                    """SELECT COALESCE(MAX(sequence),0)+1
                         FROM runtime_session_events WHERE session_id=?""",
                    (session_id,),
                ).fetchone()[0]
            )
            cursor = self.db.execute(
                """INSERT INTO runtime_session_events(
                       identity,session_id,sequence,kind,payload_json,mutable
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    self._identity("runtime-event"),
                    session_id,
                    sequence,
                    kind,
                    json.dumps(payload, sort_keys=True),
                    int(mutable),
                ),
            )
            if mutable:
                self.db.execute(
                    """UPDATE worker_sessions
                          SET mutable_action_count=mutable_action_count+1,
                              version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (session_id,),
                )
            event_id = int(cursor.lastrowid)
            self._event(
                f"runtime.event.{kind}",
                "worker_session",
                session_id,
                {
                    "event_id": event_id,
                    "sequence": sequence,
                    "mutable": mutable,
                },
            )
        return event_id

    def runtime_events(self, session_id: int, *, after_sequence: int = 0):
        self.runtime_session(session_id)
        return self.db.execute(
            """SELECT * FROM runtime_session_events
                WHERE session_id=? AND sequence>? ORDER BY sequence""",
            (session_id, after_sequence),
        ).fetchall()

    def cancel_runtime_session(self, session_id: int, *, reason: str) -> bool:
        with self.db:
            row = self.runtime_session(session_id)
            status = str(row["status"])
            if status == "cancelled":
                return False
            if status in {"succeeded", "failed"}:
                raise ValueError(f"Terminal runtime session is already {status}")
            self.db.execute(
                """UPDATE worker_sessions
                      SET status='cancelled',version=version+1,
                          updated_at=CURRENT_TIMESTAMP,finalized_at=CURRENT_TIMESTAMP,
                          result_json=? WHERE id=?""",
                (json.dumps({"status": "cancelled", "reason": reason}), session_id),
            )
            self._event(
                "runtime.session.cancelled",
                "worker_session",
                session_id,
                {"reason": reason},
            )
        return True

    def finalize_runtime_session(
        self, session_id: int, *, status: str, result: dict[str, Any]
    ) -> bool:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Runtime terminal status is invalid")
        with self.db:
            self._begin_immediate()
            row = self.runtime_session(session_id)
            if status == "succeeded":
                self._assert_admitted_runtime_session(row)
            current = str(row["status"])
            if current in {"succeeded", "failed", "cancelled"}:
                if current != status:
                    raise ValueError(
                        f"Runtime session already finalized as {current}"
                    )
                return False
            ensure_transition("worker_session", current, status)
            self.db.execute(
                """UPDATE worker_sessions
                      SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP,
                          finalized_at=CURRENT_TIMESTAMP,result_json=? WHERE id=?""",
                (status, json.dumps(result, sort_keys=True), session_id),
            )
            self._event(
                "runtime.session.finalized",
                "worker_session",
                session_id,
                {"status": status},
            )
        return True

    def runtime_fallback_allowed(self, session_id: int) -> bool:
        row = self.runtime_session(session_id)
        return int(row["mutable_action_count"]) == 0

    def runtime_settings(self):
        return self.db.execute(
            "SELECT * FROM runtime_settings ORDER BY key"
        ).fetchall()

    def update_runtime_setting(
        self, key: str, value: Any, actor: str = "Local operator"
    ) -> int:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self.db:
            current = self.db.execute(
                "SELECT value_json,version FROM runtime_settings WHERE key=?", (key,)
            ).fetchone()
            previous = json.loads(current["value_json"]) if current else None
            version = int(current["version"]) + 1 if current else 1
            self.db.execute(
                """INSERT INTO runtime_setting_versions(key,version,value_json,actor)
                     VALUES(?,?,?,?)""",
                (key, version, payload, actor),
            )
            self.db.execute(
                """INSERT INTO runtime_settings(key,value_json,version)
                     VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET
                         value_json=excluded.value_json,
                         version=excluded.version,
                         updated_at=CURRENT_TIMESTAMP""",
                (key, payload, version),
            )
            self._event(
                "settings.updated",
                "runtime_setting",
                key,
                {
                    "key": key,
                    "previous": previous,
                    "value": value,
                    "version": version,
                    "actor": actor,
                },
            )
        return version

    def create_project(self, name: str, description: str) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO projects(name,description) VALUES(?,?)", (name, description))
            project_id = int(cur.lastrowid)
            self._event("project.created", "project", project_id, {"name": name})
        return project_id

    def find_project(self, name: str):
        return self.db.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()

    def create_task(self, item: WorkItem) -> int:
        identity = self._identity("work-item")
        snapshot = json.dumps(item.to_dict())
        with self.db:
            cur = self.db.execute(
                """INSERT INTO work_items(
                       identity,project_id,title,description,payload,status,kind,
                       dependencies_json,inputs_json,expected_outputs_json,
                       acceptance_criteria_json,permissions_json,budget_max_tokens,
                       budget_max_seconds,budget_max_cost_usd,artifact_ids_json,
                       github_number,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    identity,
                    item.project_id,
                    item.title,
                    item.description,
                    snapshot,
                    item.status.value,
                    item.kind,
                    json.dumps(item.dependencies),
                    json.dumps(item.inputs),
                    json.dumps(item.expected_outputs),
                    json.dumps(item.acceptance_criteria),
                    json.dumps(item.permissions),
                    item.budget.max_tokens,
                    item.budget.max_seconds,
                    item.budget.max_cost_usd,
                    json.dumps(item.artifacts),
                    item.github_number,
                ),
            )
            item.id = int(cur.lastrowid)
            self._event("task.created", "task", item.id, item.to_dict())
        return item.id

    def get_task(self, task_id: int) -> WorkItem:
        row = self.db.execute("SELECT * FROM work_items WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown task: {task_id}")
        return WorkItem(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            status=Status(row["status"]),
            kind=str(row["kind"]),
            dependencies=json.loads(row["dependencies_json"]),
            inputs=json.loads(row["inputs_json"]),
            expected_outputs=json.loads(row["expected_outputs_json"]),
            acceptance_criteria=json.loads(row["acceptance_criteria_json"]),
            permissions=json.loads(row["permissions_json"]),
            budget=Budget(
                max_tokens=int(row["budget_max_tokens"]),
                max_seconds=int(row["budget_max_seconds"]),
                max_cost_usd=float(row["budget_max_cost_usd"]),
            ),
            artifacts=json.loads(row["artifact_ids_json"]),
            github_number=row["github_number"],
        )

    def transition_task(self, task_id: int, target: str) -> None:
        with self.db:
            row = self.db.execute(
                "SELECT status,version FROM work_items WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown task: {task_id}")
            source = str(row["status"])
            ensure_transition("work_item", source, target)
            if target == "approved":
                evidence = self.criterion_evidence_status(task_id)
                if not evidence["closed"]:
                    raise ValueError(
                        "Work item cannot be approved without accepted primary "
                        f"evidence for criteria: {evidence['missing_criteria']}"
                    )
            updated = self.db.execute(
                """UPDATE work_items
                      SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status=? AND version=?""",
                (target, task_id, source, row["version"]),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Work item {task_id} changed concurrently")
            self._event(
                f"task.{target}",
                "task",
                task_id,
                {"previous_state": source, "resulting_state": target},
            )

    def archive_task(self, task_id: int, *, reason: str = "") -> None:
        """Hide a work item from active backlog while retaining all evidence."""
        with self.db:
            row = self.db.execute(
                "SELECT status,version,inputs_json FROM work_items WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown task: {task_id}")
            inputs = json.loads(row["inputs_json"])
            if inputs.get("archived"):
                raise ValueError(f"Work item {task_id} is already archived")
            active_run = self.db.execute(
                "SELECT id FROM workflow_runs WHERE task_id=? AND status IN ('running','awaiting_approval') LIMIT 1",
                (task_id,),
            ).fetchone()
            if active_run:
                raise ValueError(f"Work item {task_id} has active workflow run {active_run['id']}")
            active_lease = self.db.execute(
                "SELECT l.id FROM leases l JOIN assignments a ON a.id=l.assignment_id WHERE a.task_id=? AND l.status='active' LIMIT 1",
                (task_id,),
            ).fetchone()
            if active_lease:
                raise ValueError(f"Work item {task_id} has active lease {active_lease['id']}")
            dependents = []
            for candidate in self.db.execute("SELECT id,dependencies_json FROM work_items WHERE id<>?", (task_id,)):
                if task_id in json.loads(candidate["dependencies_json"]):
                    dependents.append(int(candidate["id"]))
            if dependents:
                raise ValueError(f"Work item {task_id} is required by dependent items: {dependents}")
            inputs["archived"] = True
            inputs["archive_reason"] = reason.strip()
            updated = self.db.execute(
                "UPDATE work_items SET inputs_json=?,payload=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?",
                (json.dumps(inputs, sort_keys=True), json.dumps({**json.loads(self.db.execute("SELECT payload FROM work_items WHERE id=?", (task_id,)).fetchone()[0]), "inputs": inputs}, sort_keys=True), task_id, row["version"]),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Work item {task_id} changed concurrently")
            self._event("task.archived", "task", task_id, {"previous_state": row["status"], "reason": reason.strip()})

    def archive_all_tasks(self, *, reason: str = "") -> list[int]:
        """Archive every non-archived work item after validating the whole graph."""
        with self.db:
            self._expire_scheduler_leases(_timestamp(_utc(datetime.now(timezone.utc))))
            rows = self.db.execute("SELECT * FROM work_items ORDER BY id").fetchall()
            candidates = [row for row in rows if not json.loads(row["inputs_json"]).get("archived")]
            ids = {int(row["id"]) for row in candidates}
            if not ids:
                return []
            active_run = self.db.execute(
                "SELECT id,task_id FROM workflow_runs WHERE task_id IN (%s) AND status IN ('running','awaiting_approval') LIMIT 1" % ",".join("?" * len(ids)), tuple(ids)
            ).fetchone()
            if active_run:
                raise ValueError(f"Cannot archive all: active workflow run {active_run['id']} belongs to work item {active_run['task_id']}")
            active_lease = self.db.execute(
                "SELECT l.id,a.task_id FROM leases l JOIN assignments a ON a.id=l.assignment_id WHERE a.task_id IN (%s) AND l.status='active' LIMIT 1" % ",".join("?" * len(ids)), tuple(ids)
            ).fetchone()
            if active_lease:
                raise ValueError(f"Cannot archive all: active lease {active_lease['id']} belongs to work item {active_lease['task_id']}")
            for row in candidates:
                inputs = json.loads(row["inputs_json"])
                inputs["archived"] = True
                inputs["archive_reason"] = reason.strip()
                payload = json.loads(row["payload"])
                payload["inputs"] = inputs
                self.db.execute(
                    "UPDATE work_items SET inputs_json=?,payload=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?",
                    (json.dumps(inputs, sort_keys=True), json.dumps(payload, sort_keys=True), row["id"], row["version"]),
                )
                self._event("task.archived", "task", int(row["id"]), {"previous_state": row["status"], "reason": reason.strip(), "bulk": True})
            return sorted(ids)

    def task_readiness(self, task_id: int, *, now: datetime | None = None) -> tuple[str, ...]:
        """Return deterministic blockers; an empty tuple means the task is runnable."""

        current = _timestamp(_utc(now))
        row = self.db.execute(
            "SELECT id,project_id,kind,status,dependencies_json FROM work_items WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown task: {task_id}")
        return self._task_blockers(row, current)

    def _task_blockers(self, row: sqlite3.Row, current: str) -> tuple[str, ...]:
        blockers: list[str] = []
        if str(row["kind"]) != "task":
            blockers.append(f"kind:{row['kind']}")
        if str(row["status"]) != "pending":
            blockers.append(f"status:{row['status']}")
        dependency_ids = [int(value) for value in json.loads(row["dependencies_json"])]
        for dependency_id in dependency_ids:
            dependency = self.db.execute(
                "SELECT project_id,status FROM work_items WHERE id=?", (dependency_id,)
            ).fetchone()
            if not dependency or int(dependency["project_id"]) != int(row["project_id"]):
                blockers.append(f"dependency:{dependency_id}:missing")
            elif str(dependency["status"]) not in {"completed", "approved"}:
                blockers.append(
                    f"dependency:{dependency_id}:{dependency['status']}"
                )
        active = self.db.execute(
            """SELECT a.id FROM assignments a
                 JOIN leases l ON l.assignment_id=a.id
                WHERE a.task_id=? AND a.status='active' AND l.status='active'
                  AND l.expires_at>? LIMIT 1""",
            (int(row["id"]), current),
        ).fetchone()
        if active:
            blockers.append(f"assignment:{active['id']}:active")
        return tuple(blockers)

    def runnable_tasks(
        self, project_id: int | None = None, *, now: datetime | None = None
    ) -> list[WorkItem]:
        current = _timestamp(_utc(now))
        query = (
            "SELECT id,project_id,kind,status,dependencies_json FROM work_items"
        )
        parameters: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            parameters = (project_id,)
        query += " ORDER BY id"
        return [
            self.get_task(int(row["id"]))
            for row in self.db.execute(query, parameters)
            if not self._task_blockers(row, current)
        ]

    def _expire_scheduler_leases(self, current: str) -> None:
        rows = self.db.execute(
            """SELECT l.id,l.assignment_id,l.fencing_token,a.task_id
                 FROM leases l JOIN assignments a ON a.id=l.assignment_id
                WHERE l.status='active' AND l.expires_at<=?""",
            (current,),
        ).fetchall()
        for row in rows:
            self.db.execute(
                """UPDATE leases SET status='expired',version=version+1,
                          updated_at=? WHERE id=? AND status='active'""",
                (current, int(row["id"])),
            )
            self.db.execute(
                """UPDATE assignments SET status='cancelled',version=version+1,
                          updated_at=? WHERE id=? AND status='active'""",
                (current, int(row["assignment_id"])),
            )
            self._event(
                "lease.expired",
                "assignment",
                int(row["assignment_id"]),
                {
                    "task_id": int(row["task_id"]),
                    "lease_id": int(row["id"]),
                    "fencing_token": int(row["fencing_token"]),
                },
            )

    def claim_runnable_task(
        self,
        task_id: int,
        worker: str,
        runtime: str,
        *,
        ttl_seconds: int = 60,
        conflict_domains: list[str] | tuple[str, ...] | None = None,
        conflict_action: str = "serialize",
        now: datetime | None = None,
    ) -> AssignmentLease:
        from .worker_admission import registered_worker

        self._begin_immediate()
        try:
            bound_project = self.db.execute(
                """SELECT 1 FROM worker_admission_projects p
                     JOIN work_items t ON t.project_id=p.project_id WHERE t.id=?""",
                (task_id,),
            ).fetchone()
            if registered_worker(self, worker) or bound_project:
                raise PermissionError("Registered workers and projects require WorkerAdmissionService.admit")
            claim = self._claim_runnable_task_in_transaction(
                task_id, worker, runtime, ttl_seconds=ttl_seconds,
                conflict_domains=conflict_domains, conflict_action=conflict_action, now=now,
            )
            self.db.commit()
        except ConflictDomainBusyError:
            # Preserve the legacy durable conflict event; admission rolls back its whole transaction.
            self.db.commit()
            raise
        except Exception:
            self.db.rollback()
            raise
        return claim

    def _claim_runnable_task_in_transaction(
        self,
        task_id: int,
        worker: str,
        runtime: str,
        *,
        ttl_seconds: int = 60,
        conflict_domains: list[str] | tuple[str, ...] | None = None,
        conflict_action: str = "serialize",
        now: datetime | None = None,
    ) -> AssignmentLease:
        if not worker.strip() or not runtime.strip():
            raise ValueError("Worker and runtime are required")
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("Lease TTL must be between 1 and 86400 seconds")
        if conflict_action not in {"serialize", "escalate"}:
            raise ValueError("Conflict action must be serialize or escalate")
        instant = _utc(now)
        current = _timestamp(instant)
        expires_at = _timestamp(instant + timedelta(seconds=ttl_seconds))
        conflict_error: ConflictDomainBusyError | None = None
        claim: AssignmentLease | None = None
        self._assert_dispatch_allowed()
        self._expire_scheduler_leases(current)
        row = self.db.execute(
            "SELECT id,project_id,kind,status,dependencies_json FROM work_items WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown task: {task_id}")
        blockers = self._task_blockers(row, current)
        if blockers:
            raise TaskNotRunnableError(
                f"Task {task_id} is not runnable: {', '.join(blockers)}"
            )
        domains = _normalize_conflict_domains(
            int(row["project_id"]), conflict_domains
        )
        active_rows = self.db.execute(
            """SELECT DISTINCT a.id,a.task_id,d.domain
                 FROM assignments a
                 JOIN leases l ON l.assignment_id=a.id
                 JOIN assignment_conflict_domains d ON d.assignment_id=a.id
                WHERE a.status='active' AND l.status='active' AND l.expires_at>?
                ORDER BY a.id,d.domain""",
            (current,),
        ).fetchall()
        conflicting = tuple(
            sorted(
                {
                    int(active["id"])
                    for active in active_rows
                    if int(active["task_id"]) == task_id
                    or any(
                        _domains_overlap(domain, str(active["domain"]))
                        for domain in domains
                    )
                }
            )
        )
        if conflicting:
            conflict = self.db.execute(
                """INSERT INTO scheduler_conflicts(
                       identity,task_id,requested_domains_json,
                       conflicting_assignment_ids_json,action
                   ) VALUES(?,?,?,?,?)""",
                (
                    self._identity("scheduler-conflict"),
                    task_id,
                    json.dumps(domains),
                    json.dumps(conflicting),
                    conflict_action,
                ),
            )
            conflict_id = int(conflict.lastrowid)
            self._event(
                f"scheduler.conflict.{conflict_action}",
                "task",
                task_id,
                {
                    "task_id": task_id,
                    "conflict_id": conflict_id,
                    "domains": list(domains),
                    "conflicting_assignment_ids": list(conflicting),
                },
            )
            conflict_error = ConflictDomainBusyError(
                conflicting, escalated=conflict_action == "escalate"
            )
        else:
            assignment = self.db.execute(
                """INSERT INTO assignments(
                       identity,task_id,agent_id,runtime,status,updated_at
                   ) VALUES(?,?,?,?, 'active',?)""",
                (
                    self._identity("assignment"),
                    task_id,
                    worker,
                    runtime,
                    current,
                ),
            )
            assignment_id = int(assignment.lastrowid)
            self.db.executemany(
                """INSERT INTO assignment_conflict_domains(assignment_id,domain)
                   VALUES(?,?)""",
                [(assignment_id, domain) for domain in domains],
            )
            fencing_token = int(
                self.db.execute(
                    "SELECT COALESCE(MAX(fencing_token),0)+1 FROM leases"
                ).fetchone()[0]
            )
            lease = self.db.execute(
                """INSERT INTO leases(
                       identity,assignment_id,fencing_token,status,expires_at,updated_at
                   ) VALUES(?,?,?,'active',?,?)""",
                (
                    self._identity("lease"),
                    assignment_id,
                    fencing_token,
                    expires_at,
                    current,
                ),
            )
            lease_id = int(lease.lastrowid)
            self._event(
                "task.claimed",
                "task",
                task_id,
                {
                    "task_id": task_id,
                    "worker": worker,
                    "runtime": runtime,
                    "assignment_id": assignment_id,
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "expires_at": expires_at,
                    "conflict_domains": list(domains),
                },
            )
            claim = AssignmentLease(
                task_id=task_id,
                assignment_id=assignment_id,
                lease_id=lease_id,
                worker=worker,
                runtime=runtime,
                fencing_token=fencing_token,
                expires_at=expires_at,
                conflict_domains=domains,
            )
        if conflict_error:
            raise conflict_error
        if claim is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Scheduler claim did not produce a lease")
        return claim

    def _assert_fenced_lease(
        self, assignment_id: int, fencing_token: int, current: str
    ) -> sqlite3.Row:
        row = self.db.execute(
            """SELECT l.*,a.task_id,a.agent_id,a.status AS assignment_status
                 FROM leases l JOIN assignments a ON a.id=l.assignment_id
                WHERE l.assignment_id=? ORDER BY l.fencing_token DESC LIMIT 1""",
            (assignment_id,),
        ).fetchone()
        if (
            not row
            or int(row["fencing_token"]) != fencing_token
            or str(row["status"]) != "active"
            or str(row["assignment_status"]) != "active"
            or str(row["expires_at"]) <= current
        ):
            raise StaleLeaseError(
                f"Assignment {assignment_id} has no active lease for fencing token "
                f"{fencing_token}"
            )
        from .worker_admission import guard_lease

        guard_lease(self, assignment_id, fencing_token, current)
        return row

    def assert_fenced_lease(
        self, assignment_id: int, fencing_token: int, *, now: datetime | None = None
    ) -> None:
        stale: StaleLeaseError | None = None
        self._begin_immediate()
        try:
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            try:
                self._assert_fenced_lease(assignment_id, fencing_token, current)
            except StaleLeaseError as exc:
                stale = exc
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if stale:
            raise stale

    def renew_task_lease(
        self,
        assignment_id: int,
        fencing_token: int,
        *,
        ttl_seconds: int = 60,
        now: datetime | None = None,
    ) -> str:
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("Lease TTL must be between 1 and 86400 seconds")
        with self.db:
            self._begin_immediate()
            instant = _utc(now)
            current = _timestamp(instant)
            expires_at = _timestamp(instant + timedelta(seconds=ttl_seconds))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            from .worker_admission import clamp_lease_expiry

            expires_at = clamp_lease_expiry(self, assignment_id, expires_at)
            self.db.execute(
                """UPDATE leases SET expires_at=?,version=version+1,updated_at=?
                    WHERE id=?""",
                (expires_at, current, int(lease["id"])),
            )
            self._event(
                "lease.renewed",
                "assignment",
                assignment_id,
                {
                    "task_id": int(lease["task_id"]),
                    "fencing_token": fencing_token,
                    "expires_at": expires_at,
                },
            )
        return expires_at

    def release_task_lease(
        self,
        assignment_id: int,
        fencing_token: int,
        *,
        outcome: str = "succeeded",
        now: datetime | None = None,
    ) -> None:
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Assignment outcome must be succeeded, failed, or cancelled")
        with self.db:
            self._begin_immediate()
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            self.db.execute(
                """UPDATE leases SET status='released',version=version+1,updated_at=?
                    WHERE id=?""",
                (current, int(lease["id"])),
            )
            self.db.execute(
                """UPDATE assignments SET status=?,version=version+1,updated_at=?
                    WHERE id=?""",
                (outcome, current, assignment_id),
            )
            self._event(
                "lease.released",
                "assignment",
                assignment_id,
                {
                    "task_id": int(lease["task_id"]),
                    "fencing_token": fencing_token,
                    "outcome": outcome,
                },
            )

    def record_fenced_mutation(
        self,
        assignment_id: int,
        fencing_token: int,
        operation: str,
        resource: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Authorize a Control Plane artifact or commit boundary with a live fence."""

        if operation not in {"artifact", "commit"}:
            raise ValueError("Fenced operation must be artifact or commit")
        with self.db:
            self._begin_immediate()
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            self._event(
                f"assignment.{operation}.authorized",
                "assignment",
                assignment_id,
                {
                    "task_id": int(lease["task_id"]),
                    "worker": str(lease["agent_id"]),
                    "fencing_token": fencing_token,
                    "resource": resource,
                },
            )

    def create_managed_worktree(
        self,
        *,
        assignment_id: int,
        fencing_token: int,
        repository: str,
        base_sha: str,
        branch: str,
        path: str,
        attempt_id: int | None = None,
        now: datetime | None = None,
    ) -> int:
        self._begin_immediate()
        try:
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            existing = self.db.execute(
                """SELECT * FROM worktrees
                    WHERE assignment_id=? AND status<>'cleaned' ORDER BY id DESC LIMIT 1""",
                (assignment_id,),
            ).fetchone()
            scope = (repository, base_sha, branch, path, attempt_id)
            if existing:
                stored = (
                    str(existing["repository"]),
                    str(existing["base_sha"]),
                    str(existing["branch"]),
                    str(existing["path"]),
                    int(existing["attempt_id"])
                    if existing["attempt_id"] is not None
                    else None,
                )
                if stored != scope:
                    raise ValueError(
                        "Assignment already owns a different active worktree"
                    )
                self.db.commit()
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO worktrees(
                       identity,assignment_id,attempt_id,repository,base_sha,
                       branch,path,status,task_id,lease_id,fencing_token,owner,
                       reconciled_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,'provisioning',?,?,?,?,?,?)""",
                (
                    self._identity("worktree"),
                    assignment_id,
                    attempt_id,
                    repository,
                    base_sha,
                    branch,
                    path,
                    int(lease["task_id"]),
                    int(lease["id"]),
                    fencing_token,
                    str(lease["agent_id"]),
                    current,
                    current,
                ),
            )
            worktree_id = int(cursor.lastrowid)
            self._event(
                "worktree.provisioning",
                "worktree",
                worktree_id,
                {
                    "task_id": int(lease["task_id"]),
                    "assignment_id": assignment_id,
                    "lease_id": int(lease["id"]),
                    "fencing_token": fencing_token,
                    "owner": str(lease["agent_id"]),
                    "repository": repository,
                    "base_sha": base_sha,
                    "branch": branch,
                    "path": path,
                },
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return worktree_id

    def managed_worktree(self, worktree_id: int):
        row = self.db.execute(
            "SELECT * FROM worktrees WHERE id=?", (worktree_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown worktree: {worktree_id}")
        return row

    def managed_worktrees(self, repository: str | None = None):
        if repository is None:
            return self.db.execute("SELECT * FROM worktrees ORDER BY id").fetchall()
        return self.db.execute(
            "SELECT * FROM worktrees WHERE repository=? ORDER BY id",
            (repository,),
        ).fetchall()

    def transition_managed_worktree(
        self,
        worktree_id: int,
        target: str,
        *,
        retention_until: str | None = None,
        reconciled_at: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> bool:
        with self.db:
            row = self.managed_worktree(worktree_id)
            source = str(row["status"])
            timestamp = reconciled_at or datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
            if source == target:
                self.db.execute(
                    """UPDATE worktrees
                          SET reconciled_at=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?""",
                    (timestamp, worktree_id),
                )
                return False
            ensure_transition("worktree", source, target)
            self.db.execute(
                """UPDATE worktrees
                      SET status=?,retention_until=COALESCE(?,retention_until),
                          reconciled_at=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status=?""",
                (target, retention_until, timestamp, worktree_id, source),
            )
            self._event(
                f"worktree.{target}",
                "worktree",
                worktree_id,
                {
                    "task_id": int(row["task_id"]),
                    "assignment_id": int(row["assignment_id"]),
                    "previous_state": source,
                    "resulting_state": target,
                    **(details or {}),
                },
            )
        return True

    @staticmethod
    def _canonical_epoch_worktree_observation(
        value: dict[str, Any]
    ) -> tuple[str, str]:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reserve_autonomous_epoch_worktree(
        self,
        *,
        mission_id: int,
        mission_key: str,
        execution_epoch_id: int,
        epoch_number: int,
        repository_path: str,
        repository_key: str,
        base_git_commit_sha: str,
        base_checkpoint_id: int | None,
        base_checkpoint_digest: str | None,
        epoch_branch: str,
        branch_key: str,
        worktree_path: str,
        worktree_path_key: str,
        mission_segment: str,
        reservation: dict[str, Any],
        policy_version: int = 1,
        now: datetime | None = None,
    ) -> int:
        """Persist immutable epoch Git authority before any caller mutates Git."""

        current = _timestamp(_utc(now))
        reservation_json, reservation_digest = (
            self._canonical_epoch_worktree_observation(reservation)
        )
        scope = {
            "mission_id": int(mission_id),
            "mission_key": str(mission_key),
            "execution_epoch_id": int(execution_epoch_id),
            "epoch_number": int(epoch_number),
            "repository_path": str(repository_path),
            "repository_key": str(repository_key),
            "base_git_commit_sha": str(base_git_commit_sha),
            "base_checkpoint_id": base_checkpoint_id,
            "base_checkpoint_digest": base_checkpoint_digest,
            "epoch_branch": str(epoch_branch),
            "branch_key": str(branch_key),
            "worktree_path": str(worktree_path),
            "worktree_path_key": str(worktree_path_key),
            "mission_segment": str(mission_segment),
            "policy_version": int(policy_version),
        }
        authority_json = json.dumps(
            scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        authority_key = hashlib.sha256(authority_json.encode("utf-8")).hexdigest()
        self._begin_immediate()
        try:
            existing = self.db.execute(
                "SELECT * FROM autonomous_epoch_worktrees WHERE execution_epoch_id=?",
                (execution_epoch_id,),
            ).fetchone()
            if existing:
                stored = {
                    key: existing[key]
                    for key in scope
                }
                if stored != scope or str(existing["authority_key"]) != authority_key:
                    raise ValueError(
                        "Execution epoch already owns a different Git worktree authority"
                    )
                self.db.commit()
                return int(existing["id"])
            cursor = self.db.execute(
                """INSERT INTO autonomous_epoch_worktrees(
                       identity,authority_key,mission_id,mission_key,
                       execution_epoch_id,epoch_number,repository_path,
                       repository_key,base_git_commit_sha,base_checkpoint_id,
                       base_checkpoint_digest,epoch_branch,branch_key,
                       worktree_path,worktree_path_key,mission_segment,
                       policy_version,reservation_json,reservation_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("autonomous-epoch-worktree"),
                    authority_key,
                    mission_id,
                    mission_key,
                    execution_epoch_id,
                    epoch_number,
                    repository_path,
                    repository_key,
                    base_git_commit_sha,
                    base_checkpoint_id,
                    base_checkpoint_digest,
                    epoch_branch,
                    branch_key,
                    worktree_path,
                    worktree_path_key,
                    mission_segment,
                    policy_version,
                    reservation_json,
                    reservation_digest,
                    current,
                ),
            )
            worktree_id = int(cursor.lastrowid)
            self.db.execute(
                """INSERT INTO autonomous_epoch_worktree_events(
                       identity,epoch_worktree_id,sequence,status,
                       observation_json,observation_digest,reason,created_at
                   ) VALUES(?, ?, 1, 'RESERVED', ?, ?, ?, ?)""",
                (
                    self._identity("autonomous-epoch-worktree-event"),
                    worktree_id,
                    reservation_json,
                    reservation_digest,
                    "Deterministic epoch Git authority reserved before mutation",
                    current,
                ),
            )
            self._event(
                "autonomous_epoch_worktree.reserved",
                "autonomous_epoch_worktree",
                worktree_id,
                {
                    **scope,
                    "authority_key": authority_key,
                    "reservation_digest": reservation_digest,
                },
            )
            self.db.commit()
            return worktree_id
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise ValueError(
                "Epoch branch or worktree authority collides with durable state"
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _autonomous_epoch_worktree_query() -> str:
        return """SELECT authority.*,
                         event.sequence AS event_sequence,
                         event.status AS status,
                         event.observation_json AS observation_json,
                         event.observation_digest AS observation_digest,
                         event.reason AS event_reason,
                         event.created_at AS observed_at
                    FROM autonomous_epoch_worktrees authority
                    JOIN autonomous_epoch_worktree_events event
                      ON event.id=(
                          SELECT latest.id
                            FROM autonomous_epoch_worktree_events latest
                           WHERE latest.epoch_worktree_id=authority.id
                           ORDER BY latest.sequence DESC LIMIT 1
                      )"""

    def autonomous_epoch_worktree(self, worktree_id: int):
        row = self.db.execute(
            self._autonomous_epoch_worktree_query() + " WHERE authority.id=?",
            (worktree_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown autonomous epoch worktree: {worktree_id}")
        return row

    def autonomous_epoch_worktree_for_epoch(self, execution_epoch_id: int):
        row = self.db.execute(
            self._autonomous_epoch_worktree_query()
            + " WHERE authority.execution_epoch_id=?",
            (execution_epoch_id,),
        ).fetchone()
        if not row:
            raise KeyError(
                f"Execution epoch has no worktree authority: {execution_epoch_id}"
            )
        return row

    def autonomous_epoch_worktrees(self, repository_key: str | None = None):
        query = self._autonomous_epoch_worktree_query()
        parameters: tuple[Any, ...] = ()
        if repository_key is not None:
            query += " WHERE authority.repository_key=?"
            parameters = (repository_key,)
        query += " ORDER BY authority.mission_id,authority.epoch_number,authority.id"
        return self.db.execute(query, parameters).fetchall()

    def append_autonomous_epoch_worktree_event(
        self,
        worktree_id: int,
        *,
        status: str,
        observation: dict[str, Any],
        reason: str,
        now: datetime | None = None,
    ) -> int:
        normalized_status = str(status).strip().upper()
        if normalized_status not in {
            "PROVISIONING",
            "READY",
            "DIRTY",
            "MISSING",
            "CONFLICT",
        }:
            raise ValueError("Invalid autonomous epoch worktree observation status")
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("Epoch worktree observation reason is required")
        observation_json, observation_digest = (
            self._canonical_epoch_worktree_observation(observation)
        )
        current = _timestamp(_utc(now))
        self._begin_immediate()
        try:
            authority = self.db.execute(
                """SELECT id,mission_id,execution_epoch_id
                     FROM autonomous_epoch_worktrees WHERE id=?""",
                (worktree_id,),
            ).fetchone()
            if not authority:
                raise KeyError(f"Unknown autonomous epoch worktree: {worktree_id}")
            sequence = int(
                self.db.execute(
                    """SELECT COALESCE(MAX(sequence),0)+1
                         FROM autonomous_epoch_worktree_events
                        WHERE epoch_worktree_id=?""",
                    (worktree_id,),
                ).fetchone()[0]
            )
            cursor = self.db.execute(
                """INSERT INTO autonomous_epoch_worktree_events(
                       identity,epoch_worktree_id,sequence,status,
                       observation_json,observation_digest,reason,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    self._identity("autonomous-epoch-worktree-event"),
                    worktree_id,
                    sequence,
                    normalized_status,
                    observation_json,
                    observation_digest,
                    normalized_reason,
                    current,
                ),
            )
            event_id = int(cursor.lastrowid)
            self._event(
                f"autonomous_epoch_worktree.{normalized_status.casefold()}",
                "autonomous_epoch_worktree",
                worktree_id,
                {
                    "mission_id": int(authority["mission_id"]),
                    "execution_epoch_id": int(authority["execution_epoch_id"]),
                    "sequence": sequence,
                    "observation_digest": observation_digest,
                    "reason": normalized_reason,
                },
            )
            self.db.commit()
            return event_id
        except Exception:
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()

    def integrity_check(self) -> dict[str, Any]:
        messages = [str(row[0]) for row in self.db.execute("PRAGMA integrity_check")]
        audit = self.verify_audit_chain()
        evidence = self.verify_evidence_ledger()
        return {
            "ok": messages == ["ok"] and audit["ok"] and evidence["ok"],
            "messages": messages,
            "audit": audit,
            "evidence": evidence,
        }

    def online_backup(self, destination: Path) -> Path:
        destination = destination.resolve()
        if destination == self.path.resolve():
            raise ValueError("Backup destination must differ from the live database")
        if destination.exists():
            raise FileExistsError(f"Backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        target = sqlite3.connect(temporary)
        try:
            self.db.backup(target)
            messages = [str(row[0]) for row in target.execute("PRAGMA integrity_check")]
            if messages != ["ok"]:
                raise sqlite3.DatabaseError(f"Backup integrity check failed: {messages}")
            target.close()
            os.replace(temporary, destination)
        except Exception:
            target.close()
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def stale_workflow_runs(self, older_than_seconds: int) -> list[sqlite3.Row]:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")
        threshold = f"-{int(older_than_seconds)} seconds"
        return self.db.execute(
            """SELECT * FROM workflow_runs
                 WHERE status IN ('running','awaiting_approval')
                   AND created_at <= datetime('now', ?)
                 ORDER BY created_at,id""",
            (threshold,),
        ).fetchall()

    def stale_provider_attempts(self, older_than_seconds: int) -> list[sqlite3.Row]:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")
        threshold = f"-{int(older_than_seconds)} seconds"
        return self.db.execute(
            """SELECT * FROM provider_execution_attempts
                 WHERE status IN ('claimed','running')
                   AND COALESCE(heartbeat_at,started_at,created_at) <= datetime('now', ?)
                 ORDER BY COALESCE(heartbeat_at,started_at,created_at),id""",
            (threshold,),
        ).fetchall()

    def start_run(self, project_id: int, task_id: int | None, workflow_id: str) -> int:
        self._assert_dispatch_allowed()
        if task_id is None:
            raise ValueError("Workflow requires a persisted work item")
        task = self.db.execute("SELECT project_id FROM work_items WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise KeyError(f"Unknown task: {task_id}")
        if int(task["project_id"]) != project_id:
            raise ValueError(f"Task {task_id} does not belong to project {project_id}")
        with self.db:
            try:
                cur = self.db.execute(
                    """INSERT INTO workflow_runs(
                           identity,project_id,task_id,workflow_id,status
                       ) VALUES(?,?,?,?, 'running')""",
                    (self._identity("run"), project_id, task_id, workflow_id),
                )
                run_id = int(cur.lastrowid)
                self.db.execute(
                    """INSERT INTO workflow_stages(identity,run_id,stage_key,status)
                       VALUES(?,?,?,'running')""",
                    (self._identity("stage"), run_id, "workflow"),
                )
                self.db.execute(
                    "INSERT INTO active_workflow_claims(task_id,workflow_id,run_id) VALUES(?,?,?)",
                    (task_id, workflow_id, run_id),
                )
                self._event("workflow.started", "run", run_id, {"workflow": workflow_id})
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Active workflow already exists for task {task_id} and workflow {workflow_id}") from exc
        return run_id

    def start_durable_run(
        self,
        *,
        project_id: int,
        task_id: int,
        workflow_id: str,
        workflow_version: str,
        definition: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> int:
        self._assert_dispatch_allowed()
        task = self.db.execute(
            "SELECT project_id FROM work_items WHERE id=?", (task_id,)
        ).fetchone()
        if not task:
            raise KeyError(f"Unknown task: {task_id}")
        if int(task["project_id"]) != project_id:
            raise ValueError(f"Task {task_id} does not belong to project {project_id}")
        definition_json = json.dumps(
            definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        definition_digest = hashlib.sha256(definition_json.encode("utf-8")).hexdigest()
        with self.db:
            try:
                cur = self.db.execute(
                    """INSERT INTO workflow_runs(
                           identity,project_id,task_id,workflow_id,status,
                           workflow_version,definition_digest,definition_json
                       ) VALUES(?,?,?,?, 'running',?,?,?)""",
                    (
                        self._identity("run"),
                        project_id,
                        task_id,
                        workflow_id,
                        workflow_version,
                        definition_digest,
                        definition_json,
                    ),
                )
                run_id = int(cur.lastrowid)
                self.db.execute(
                    "INSERT INTO active_workflow_claims(task_id,workflow_id,run_id) VALUES(?,?,?)",
                    (task_id, workflow_id, run_id),
                )
                for stage in stages:
                    self.db.execute(
                        """INSERT INTO workflow_stages(
                               identity,run_id,stage_key,status,dependencies_json,
                               definition_json
                           ) VALUES(?,?,?,'pending',?,?)""",
                        (
                            self._identity("stage"),
                            run_id,
                            stage["id"],
                            json.dumps(stage.get("depends_on", []), separators=(",", ":")),
                            json.dumps(
                                stage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                            ),
                        ),
                    )
                self._event(
                    "workflow.started",
                    "run",
                    run_id,
                    {
                        "task_id": task_id,
                        "workflow": workflow_id,
                        "workflow_version": workflow_version,
                        "definition_digest": definition_digest,
                    },
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Active workflow already exists for task {task_id} and workflow {workflow_id}"
                ) from exc
        return run_id

    def durable_run(self, run_id: int):
        row = self.db.execute(
            "SELECT * FROM workflow_runs WHERE id=? AND definition_digest IS NOT NULL",
            (run_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown durable workflow run: {run_id}")
        return row

    def durable_stages(self, run_id: int):
        self.durable_run(run_id)
        return self.db.execute(
            "SELECT * FROM workflow_stages WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

    def transition_durable_stage(
        self, run_id: int, stage_key: str, target: str, payload: dict[str, Any]
    ) -> None:
        with self.db:
            row = self.db.execute(
                """SELECT s.*,r.checkpoint_sequence,r.task_id
                     FROM workflow_stages s
                     JOIN workflow_runs r ON r.id=s.run_id
                    WHERE s.run_id=? AND s.stage_key=?""",
                (run_id, stage_key),
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown stage {stage_key} for run {run_id}")
            source = str(row["status"])
            ensure_transition("stage", source, target)
            if target == "running":
                succeeded = {
                    value[0]
                    for value in self.db.execute(
                        "SELECT stage_key FROM workflow_stages WHERE run_id=? AND status='succeeded'",
                        (run_id,),
                    )
                }
                missing = set(json.loads(row["dependencies_json"])) - succeeded
                if missing:
                    raise ValueError(f"Stage {stage_key} dependencies are incomplete: {sorted(missing)}")
            sequence = int(row["checkpoint_sequence"]) + 1
            payload_json = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            updated = self.db.execute(
                """UPDATE workflow_stages
                      SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status=?""",
                (target, row["id"], source),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Stage {stage_key} changed concurrently")
            self.db.execute(
                "UPDATE workflow_runs SET checkpoint_sequence=? WHERE id=?",
                (sequence, run_id),
            )
            self.db.execute(
                """INSERT INTO stage_checkpoints(
                       identity,run_id,stage_id,sequence,state,payload_json,payload_digest
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    self._identity("checkpoint"),
                    run_id,
                    row["id"],
                    sequence,
                    target,
                    payload_json,
                    payload_digest,
                ),
            )
            self._event(
                f"stage.{target}",
                "stage",
                row["id"],
                {
                    "task_id": row["task_id"],
                    "run_id": run_id,
                    "stage_id": stage_key,
                    "sequence": sequence,
                    "payload_digest": payload_digest,
                },
            )

    def reserve_workflow_mutation(
        self,
        *,
        run_id: int,
        stage_key: str,
        operation: str,
        idempotency_key: str,
        request: dict[str, Any],
        reconciliation_policy: str | None = None,
    ):
        operation = str(getattr(operation, "value", operation))
        if operation not in WORKFLOW_MUTATION_OPERATIONS:
            raise ValueError(operation)
        if not idempotency_key.strip():
            raise ValueError("Mutation idempotency key is required")
        if reconciliation_policy is None:
            reconciliation_policy = (
                "idempotent_replay"
                if operation == "provider_call"
                else "verify_then_retry"
            )
        reconciliation_policy = str(
            getattr(reconciliation_policy, "value", reconciliation_policy)
        )
        if reconciliation_policy not in WORKFLOW_MUTATION_RECONCILIATION_POLICIES:
            raise ValueError(reconciliation_policy)
        request_json = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        request_digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        legacy_request_json = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_request_digest = hashlib.sha256(
            legacy_request_json.encode("utf-8")
        ).hexdigest()
        empty_json = "{}"
        empty_digest = hashlib.sha256(empty_json.encode("utf-8")).hexdigest()
        created = False
        with self.db:
            stage = self.db.execute(
                "SELECT id FROM workflow_stages WHERE run_id=? AND stage_key=?",
                (run_id, stage_key),
            ).fetchone()
            if not stage:
                raise KeyError(f"Unknown stage {stage_key} for run {run_id}")
            existing = self.db.execute(
                """SELECT * FROM workflow_mutations
                    WHERE run_id=? AND operation=? AND idempotency_key=?""",
                (run_id, operation, idempotency_key),
            ).fetchone()
            if existing:
                if (
                    int(existing["stage_id"]) != int(stage["id"])
                    or existing["request_digest"]
                    not in {request_digest, legacy_request_digest}
                    or existing["reconciliation_policy"] != reconciliation_policy
                ):
                    raise ValueError("Idempotency key was already bound to a different mutation")
                return existing, created
            cur = self.db.execute(
                """INSERT INTO workflow_mutations(
                       identity,run_id,stage_id,operation,idempotency_key,
                       request_json,request_digest,reconciliation_policy,
                       evidence_json,evidence_digest
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("workflow-mutation"),
                    run_id,
                    stage["id"],
                    operation,
                    idempotency_key,
                    request_json,
                    request_digest,
                    reconciliation_policy,
                    empty_json,
                    empty_digest,
                ),
            )
            mutation_id = int(cur.lastrowid)
            created = True
            self._event(
                "workflow.mutation.reserved",
                "workflow_mutation",
                mutation_id,
                {
                    "run_id": run_id,
                    "stage_id": stage_key,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                    "request_digest": request_digest,
                    "reconciliation_policy": reconciliation_policy,
                },
            )
        return self.db.execute(
            "SELECT * FROM workflow_mutations WHERE id=?", (mutation_id,)
        ).fetchone(), created

    def transition_workflow_mutation(
        self,
        mutation_id: int,
        target: str,
        *,
        result: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        target = str(getattr(target, "value", target))
        if target not in WORKFLOW_MUTATION_LIFECYCLES - {"reserved"}:
            raise ValueError(target)
        with self.db:
            row = self.db.execute(
                "SELECT * FROM workflow_mutations WHERE id=?", (mutation_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown workflow mutation: {mutation_id}")
            result_json = (
                json.dumps(
                    result,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if result is not None
                else row["result_json"]
            )
            result_digest = (
                hashlib.sha256(str(result_json).encode("utf-8")).hexdigest()
                if result_json is not None
                else None
            )
            evidence_json = (
                json.dumps(
                    evidence,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if evidence is not None
                else str(row["evidence_json"])
            )
            evidence_digest = hashlib.sha256(
                evidence_json.encode("utf-8")
            ).hexdigest()
            if row["status"] == target:
                stored_result_digest = row["result_digest"]
                if stored_result_digest is None and result_json is not None:
                    stored_result_digest = result_digest
                if (
                    row["result_json"] != result_json
                    or stored_result_digest != result_digest
                    or row["evidence_json"] != evidence_json
                    or row["evidence_digest"] != evidence_digest
                ):
                    raise ValueError("Workflow mutation lifecycle result is immutable")
                return False
            terminal = target in {
                "completed",
                "failed",
                "reconciled",
                "needs_attention",
            }
            updated = self.db.execute(
                """UPDATE workflow_mutations
                      SET status=?,result_json=?,result_digest=?,
                          evidence_json=?,evidence_digest=?,
                          updated_at=CURRENT_TIMESTAMP,
                          completed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
                    WHERE id=? AND status=?""",
                (
                    target,
                    result_json,
                    result_digest,
                    evidence_json,
                    evidence_digest,
                    terminal,
                    mutation_id,
                    row["status"],
                ),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Workflow mutation {mutation_id} changed concurrently")
            self._event(
                f"workflow.mutation.{target}",
                "workflow_mutation",
                mutation_id,
                {
                    "run_id": row["run_id"],
                    "operation": row["operation"],
                    "result_digest": result_digest,
                    "evidence_digest": evidence_digest,
                },
            )
        return True

    def complete_workflow_mutation(
        self,
        mutation_id: int,
        result: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        row = self.db.execute(
            "SELECT * FROM workflow_mutations WHERE id=?", (mutation_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown workflow mutation: {mutation_id}")
        if row["status"] in {"completed", "reconciled"}:
            result_json = json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            try:
                same_result = json.loads(str(row["result_json"])) == result
            except (TypeError, json.JSONDecodeError):
                same_result = False
            if not same_result:
                raise ValueError("Completed mutation result is immutable")
            return False
        return self.transition_workflow_mutation(
            mutation_id,
            "completed",
            result=result,
            evidence=evidence,
        )

    def _transition_run(self, run_id: int, target: str, *, event_payload: dict[str, Any] | None = None) -> None:
        row = self.db.execute("SELECT status,task_id,workflow_id FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown run: {run_id}")
        source = str(row["status"])
        ensure_transition("run", source, target)
        completed = "CURRENT_TIMESTAMP" if target in {"approved", "rejected", "failed"} else "NULL"
        updated = self.db.execute(
            f"UPDATE workflow_runs SET status=?,version=version+1,completed_at={completed} WHERE id=? AND status=?",
            (target, run_id, source),
        )
        if updated.rowcount != 1:
            raise ValueError(f"Workflow run {run_id} changed concurrently")
        stage_target = {
            "awaiting_approval": "waiting_approval",
            "approved": "succeeded",
            "rejected": "failed",
            "failed": "failed",
        }[target]
        self.db.execute(
            """UPDATE workflow_stages
                  SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND stage_key='workflow'
                  AND status IN ('running','waiting_approval')""",
            (stage_target, run_id),
        )
        if target in {"approved", "rejected", "failed"}:
            self.db.execute("DELETE FROM active_workflow_claims WHERE run_id=?", (run_id,))
            self.db.execute(
                """INSERT OR IGNORE INTO active_workflow_claims(task_id,workflow_id,run_id)
                   SELECT task_id,workflow_id,MIN(id) FROM workflow_runs
                    WHERE task_id=? AND workflow_id=?
                      AND status IN ('running','awaiting_approval')
                    GROUP BY task_id,workflow_id""",
                (row["task_id"], row["workflow_id"]),
            )
        self._event(f"workflow.{target}", "run", run_id, event_payload or {})

    def finish_run(self, run_id: int, status: str, *, event_payload: dict[str, Any] | None = None) -> None:
        with self.db:
            self._transition_run(run_id, status, event_payload=event_payload)

    def create_approval_gate(self, run_id: int) -> int:
        with self.db:
            self._transition_run(run_id, "awaiting_approval")
            cur = self.db.execute("INSERT INTO approval_gates(run_id) VALUES(?)", (run_id,))
            gate_id = int(cur.lastrowid)
            self._event("approval.requested", "approval", gate_id, {"run_id": run_id})
        return gate_id

    def approvals(self):
        return self.db.execute("SELECT * FROM approval_gates ORDER BY id").fetchall()

    def decide_approval(
        self, gate_id: int, decision: str, note: str, actor: str = "Founder"
    ) -> bool:
        if decision not in {"approved", "rejected"}:
            raise ValueError(decision)
        with self.db:
            gate = self.db.execute("SELECT run_id,status FROM approval_gates WHERE id=?", (gate_id,)).fetchone()
            if not gate:
                raise KeyError(f"Unknown approval: {gate_id}")
            if gate["status"] == decision:
                return False
            if gate["status"] != "pending":
                raise ValueError(f"Approval {gate_id} is already {gate['status']}")
            updated = self.db.execute(
                "UPDATE approval_gates SET status=?,decision_note=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (decision, note, gate_id),
            )
            if updated.rowcount != 1:
                current = self.db.execute(
                    "SELECT status FROM approval_gates WHERE id=?", (gate_id,)
                ).fetchone()
                if current and current["status"] == decision:
                    return False
                raise ValueError(f"Approval {gate_id} was decided concurrently")
            self._transition_run(int(gate["run_id"]), decision, event_payload={"approval_gate_id": gate_id})
            decided_at = self.db.execute(
                "SELECT decided_at FROM approval_gates WHERE id=?", (gate_id,)
            ).fetchone()[0]
            self._event(
                f"approval.{decision}",
                "approval",
                gate_id,
                {
                    "note": note,
                    "actor": actor,
                    "timestamp": decided_at,
                    "target": {"type": "workflow_run", "id": int(gate["run_id"])},
                    "previous_state": "pending",
                    "resulting_state": decision,
                },
            )
        return True

    def add_artifact(
        self,
        run_id: int,
        stage: str,
        agent_id: str,
        provider: str,
        content: str,
        *,
        producer: dict[str, Any] | None = None,
        verifier: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        toolchain: dict[str, Any] | None = None,
        evidence_kind: str = "review",
    ) -> int:
        if evidence_kind not in {"test_result", "diff", "review", "summary"}:
            raise ValueError(f"Unknown evidence kind: {evidence_kind}")
        producer = producer or {"agent_id": agent_id, "provider": provider}
        verifier = verifier or {"status": "unverified"}
        inputs = inputs or {"run_id": run_id, "stage": stage}
        toolchain = toolchain or {"provider": provider}
        with self.db:
            return self._insert_artifact(
                run_id,
                stage,
                agent_id,
                provider,
                content,
                producer,
                verifier,
                inputs,
                toolchain,
                evidence_kind,
            )

    def _insert_artifact(
        self,
        run_id: int,
        stage: str,
        agent_id: str,
        provider: str,
        content: str,
        producer: dict[str, Any],
        verifier: dict[str, Any],
        inputs: dict[str, Any],
        toolchain: dict[str, Any],
        evidence_kind: str,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO artifacts(
                   identity,run_id,stage,agent_id,provider,content,digest,
                   producer_json,verifier_json,inputs_json,toolchain_json,
                   evidence_kind
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self._identity("artifact"),
                run_id,
                stage,
                agent_id,
                provider,
                content,
                self._content_digest(content),
                json.dumps(producer, sort_keys=True),
                json.dumps(verifier, sort_keys=True),
                json.dumps(inputs, sort_keys=True),
                json.dumps(toolchain, sort_keys=True),
                evidence_kind,
            ),
        )
        artifact_id = int(cur.lastrowid)
        self._event(
            "artifact.created",
            "artifact",
            artifact_id,
            {"stage": stage, "provider": provider},
        )
        return artifact_id

    def add_fenced_artifact(
        self,
        assignment_id: int,
        fencing_token: int,
        run_id: int,
        stage: str,
        provider: str,
        content: str,
        *,
        evidence_kind: str = "review",
        now: datetime | None = None,
    ) -> int:
        """Atomically verify assignment authority and persist a worker artifact."""

        if evidence_kind not in {"test_result", "diff", "review", "summary"}:
            raise ValueError(f"Unknown evidence kind: {evidence_kind}")
        with self.db:
            self._begin_immediate()
            current = _timestamp(_utc(now))
            self._expire_scheduler_leases(current)
            lease = self._assert_fenced_lease(
                assignment_id, fencing_token, current
            )
            run = self.db.execute(
                "SELECT task_id FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(f"Unknown run: {run_id}")
            if int(run["task_id"]) != int(lease["task_id"]):
                raise PermissionError("Assignment lease does not own the workflow task")
            from .worker_admission import admission_for_assignment

            admitted = admission_for_assignment(self, assignment_id)
            if admitted is not None and (
                admitted["run_id"] != run_id or admitted["stage_key"] != stage
                or admitted["provider_id"] != provider
            ):
                raise PermissionError("Artifact does not match its admitted run, stage and provider")
            worker = str(lease["agent_id"])
            artifact_id = self._insert_artifact(
                run_id,
                stage,
                worker,
                provider,
                content,
                {
                    "agent_id": worker,
                    "provider": provider,
                    "assignment_id": assignment_id,
                    "fencing_token": fencing_token,
                },
                {"status": "unverified"},
                {
                    "run_id": run_id,
                    "stage": stage,
                    "assignment_id": assignment_id,
                    "fencing_token": fencing_token,
                },
                {"provider": provider},
                evidence_kind,
            )
            self._event(
                "assignment.artifact.authorized",
                "assignment",
                assignment_id,
                {
                    "task_id": int(lease["task_id"]),
                    "artifact_id": artifact_id,
                    "fencing_token": fencing_token,
                },
            )
            return artifact_id

    def latest_run(self):
        return self.db.execute("SELECT * FROM workflow_runs ORDER BY id DESC LIMIT 1").fetchone()

    def artifacts(self, run_id: int):
        return self.db.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def reviewer_assignments(self, run_id: int | None = None):
        if run_id is None:
            return self.db.execute(
                "SELECT * FROM reviewer_assignments ORDER BY id"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM reviewer_assignments WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()

    def reviewer_usage(
        self, stage: str, candidate_ids: list[str]
    ) -> dict[str, tuple[int, int]]:
        if not candidate_ids:
            return {}
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.db.execute(
            f"""SELECT reviewer_agent_id,COUNT(*) AS uses,MAX(id) AS last_id
                   FROM reviewer_assignments
                  WHERE stage=? AND reviewer_agent_id IN ({placeholders})
                  GROUP BY reviewer_agent_id""",
            (stage, *candidate_ids),
        ).fetchall()
        return {
            str(row["reviewer_agent_id"]): (int(row["uses"]), int(row["last_id"]))
            for row in rows
        }

    def latest_reviewer_assignment(self, stage: str):
        return self.db.execute(
            """SELECT * FROM reviewer_assignments
                 WHERE stage=? ORDER BY id DESC LIMIT 1""",
            (stage,),
        ).fetchone()

    def record_reviewer_assignment(
        self,
        *,
        run_id: int,
        stage: str,
        reviewer: Any,
        subjects: list[Any],
        excluded_models: list[str],
        excluded_candidates: dict[str, str],
        strategy: str,
    ) -> int:
        producer_agents = [
            {
                "stage": subject.stage,
                "agent_id": subject.producer.id,
                "provider": subject.producer.provider,
                "model": subject.producer.model_identity,
            }
            for subject in subjects
        ]
        with self.db:
            cur = self.db.execute(
                """INSERT INTO reviewer_assignments(
                       run_id,stage,reviewer_agent_id,reviewer_provider,reviewer_model,
                       reviewed_stages,reviewed_artifact_ids,producer_agents,
                       excluded_models,excluded_candidates,strategy
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    stage,
                    reviewer.id,
                    reviewer.provider,
                    reviewer.model_identity,
                    json.dumps([subject.stage for subject in subjects]),
                    json.dumps([subject.artifact_id for subject in subjects]),
                    json.dumps(producer_agents, sort_keys=True),
                    json.dumps(excluded_models),
                    json.dumps(excluded_candidates, sort_keys=True),
                    strategy,
                ),
            )
            assignment_id = int(cur.lastrowid)
            self._event(
                "reviewer.assigned",
                "review_assignment",
                assignment_id,
                {
                    "run_id": run_id,
                    "stage": stage,
                    "reviewer_agent_id": reviewer.id,
                    "reviewer_provider": reviewer.provider,
                    "reviewer_model": reviewer.model_identity,
                    "producer_agents": producer_agents,
                    "strategy": strategy,
                },
            )
        return assignment_id

    def complete_reviewer_assignment(
        self,
        *,
        run_id: int,
        stage: str,
        review_artifact_id: int,
        verdict: str,
    ) -> None:
        with self.db:
            updated = self.db.execute(
                """UPDATE reviewer_assignments
                      SET review_artifact_id=?,verdict=?,completed_at=CURRENT_TIMESTAMP
                    WHERE run_id=? AND stage=? AND completed_at IS NULL""",
                (review_artifact_id, verdict, run_id, stage),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    f"Reviewer assignment for run {run_id} stage {stage} is missing or complete"
                )
            assignment = self.db.execute(
                "SELECT id,reviewer_agent_id,reviewer_model FROM reviewer_assignments WHERE run_id=? AND stage=?",
                (run_id, stage),
            ).fetchone()
            self._event(
                "reviewer.review.completed",
                "review_assignment",
                int(assignment["id"]),
                {
                    "review_artifact_id": review_artifact_id,
                    "reviewer_agent_id": assignment["reviewer_agent_id"],
                    "reviewer_model": assignment["reviewer_model"],
                    "verdict": verdict,
                },
            )

    def review_artifact(self, artifact_id: int, status: str, note: str) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError(status)
        with self.db:
            ensure_transition("artifact", "pending", status)
            updated = self.db.execute(
                """UPDATE artifacts
                      SET status=?,review_note=?,version=version+1,verifier_json=?
                    WHERE id=? AND status='pending'""",
                (
                    status,
                    note,
                    json.dumps(
                        {"type": "human_artifact_review", "decision": status, "note": note},
                        sort_keys=True,
                    ),
                    artifact_id,
                ),
            )
            if updated.rowcount != 1:
                row = self.db.execute("SELECT status FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                if not row:
                    raise KeyError(f"Unknown artifact: {artifact_id}")
                raise ValueError(f"Artifact {artifact_id} is already {row['status']}")
            self._event(f"artifact.{status}", "artifact", artifact_id, {"note": note})

    def link_criterion_evidence(
        self,
        *,
        task_id: int,
        criterion_index: int,
        artifact_id: int,
        evidence_type: str,
    ) -> int:
        if evidence_type not in {"test_result", "diff", "review", "summary"}:
            raise ValueError(f"Unknown evidence type: {evidence_type}")
        task = self.get_task(task_id)
        if criterion_index < 0 or criterion_index >= len(task.acceptance_criteria):
            raise ValueError(f"Unknown criterion index {criterion_index} for task {task_id}")
        artifact = self.db.execute(
            """SELECT a.*,r.task_id
                 FROM artifacts a
                 JOIN workflow_runs r ON r.id=a.run_id
                WHERE a.id=?""",
            (artifact_id,),
        ).fetchone()
        if not artifact:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if int(artifact["task_id"]) != task_id:
            raise ValueError("Evidence artifact belongs to a different work item")
        if artifact["evidence_kind"] != evidence_type:
            raise ValueError(
                f"Artifact is typed {artifact['evidence_kind']}, not {evidence_type}"
            )
        with self.db:
            existing = self.db.execute(
                """SELECT id FROM criterion_evidence
                    WHERE task_id=? AND criterion_index=? AND artifact_id=?
                      AND evidence_type=?""",
                (task_id, criterion_index, artifact_id, evidence_type),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = self.db.execute(
                """INSERT INTO criterion_evidence(
                       identity,task_id,criterion_index,criterion_text,artifact_id,
                       evidence_type,primary_evidence,artifact_digest
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    self._identity("criterion-evidence"),
                    task_id,
                    criterion_index,
                    task.acceptance_criteria[criterion_index],
                    artifact_id,
                    evidence_type,
                    int(evidence_type != "summary"),
                    artifact["digest"],
                ),
            )
            evidence_id = int(cur.lastrowid)
            self._event(
                "evidence.linked",
                "criterion_evidence",
                evidence_id,
                {
                    "task_id": task_id,
                    "run_id": artifact["run_id"],
                    "stage": artifact["stage"],
                    "artifact_id": artifact_id,
                    "criterion_index": criterion_index,
                    "evidence_type": evidence_type,
                    "primary_evidence": evidence_type != "summary",
                },
            )
        return evidence_id

    def decide_criterion_evidence(
        self,
        evidence_id: int,
        decision: str,
        *,
        verifier: str,
        note: str = "",
    ) -> bool:
        if decision not in {"accepted", "rejected"}:
            raise ValueError(decision)
        if not verifier.strip():
            raise ValueError("Evidence verifier is required")
        with self.db:
            row = self.db.execute(
                """SELECT e.*,a.content,a.digest,a.run_id,a.stage
                     FROM criterion_evidence e
                     JOIN artifacts a ON a.id=e.artifact_id
                    WHERE e.id=?""",
                (evidence_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown criterion evidence: {evidence_id}")
            if row["status"] == decision:
                return False
            if row["status"] != "proposed":
                raise ValueError(f"Evidence {evidence_id} is already {row['status']}")
            current_digest = self._content_digest(str(row["content"]))
            if current_digest != row["artifact_digest"] or current_digest != row["digest"]:
                raise ValueError("Evidence artifact digest changed before verification")
            verifier_record = json.dumps(
                {"verifier": verifier, "decision": decision, "note": note},
                sort_keys=True,
            )
            if decision == "accepted":
                already_locked = self.db.execute(
                    """SELECT 1 FROM criterion_evidence
                        WHERE artifact_id=? AND status='accepted' LIMIT 1""",
                    (row["artifact_id"],),
                ).fetchone()
                if not already_locked:
                    self.db.execute(
                        "UPDATE artifacts SET verifier_json=? WHERE id=?",
                        (verifier_record, row["artifact_id"]),
                    )
            updated = self.db.execute(
                """UPDATE criterion_evidence
                      SET status=?,verifier_json=?,version=version+1,
                          decided_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status='proposed'""",
                (decision, verifier_record, evidence_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Evidence {evidence_id} changed concurrently")
            self._event(
                f"evidence.{decision}",
                "criterion_evidence",
                evidence_id,
                {
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "stage": row["stage"],
                    "artifact_id": row["artifact_id"],
                    "criterion_index": row["criterion_index"],
                    "evidence_type": row["evidence_type"],
                    "verifier": verifier,
                    "note": note,
                },
            )
        return True

    def criterion_evidence_status(self, task_id: int) -> dict[str, Any]:
        task = self.get_task(task_id)
        rows = self.db.execute(
            """SELECT * FROM criterion_evidence
                WHERE task_id=? ORDER BY criterion_index,id""",
            (task_id,),
        ).fetchall()
        accepted_primary = {
            int(row["criterion_index"])
            for row in rows
            if row["status"] == "accepted" and int(row["primary_evidence"]) == 1
        }
        missing = [
            {"index": index, "criterion": criterion}
            for index, criterion in enumerate(task.acceptance_criteria)
            if index not in accepted_primary
        ]
        return {
            "task_id": task_id,
            "closed": not missing,
            "missing_criteria": missing,
            "evidence": [
                {
                    "id": int(row["id"]),
                    "criterion_index": int(row["criterion_index"]),
                    "artifact_id": int(row["artifact_id"]),
                    "evidence_type": str(row["evidence_type"]),
                    "primary": bool(row["primary_evidence"]),
                    "status": str(row["status"]),
                    "digest": str(row["artifact_digest"]),
                }
                for row in rows
            ],
        }

    # Provider approvals bind immutable snapshots to durable, one-use attempts.
    def request_provider_execution(
        self,
        provider: str,
        agent_id: str,
        task_id: int,
        request_hash: str,
        definition_hash: str,
    ) -> int:
        request_hash = _sha256_snapshot(request_hash, "request_hash")
        definition_hash = _sha256_snapshot(definition_hash, "definition_hash")
        if not self.db.execute("SELECT id FROM work_items WHERE id=?", (task_id,)).fetchone():
            raise KeyError(f"Unknown task: {task_id}")
        with self.db:
            try:
                cur = self.db.execute(
                    """INSERT INTO provider_execution_gates(
                           provider,agent_id,task_id,request_hash,definition_hash
                       ) VALUES(?,?,?,?,?)""",
                    (provider, agent_id, task_id, request_hash, definition_hash),
                )
                gate_id = int(cur.lastrowid)
                self.db.execute(
                    "INSERT INTO pending_provider_gate_claims(provider,agent_id,task_id,gate_id) VALUES(?,?,?,?)",
                    (provider, agent_id, task_id, gate_id),
                )
                self._event(
                    "provider.execution.requested",
                    "provider_gate",
                    gate_id,
                    {
                        "provider": provider,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "request_hash": request_hash,
                        "definition_hash": definition_hash,
                    },
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Pending provider gate already exists for {provider}/{agent_id}/task {task_id}") from exc
        return gate_id

    def provider_execution_gates(self):
        return self.db.execute("SELECT * FROM provider_execution_gates ORDER BY id").fetchall()

    def decide_provider_execution(self, gate_id: int, decision: str, note: str) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError(decision)
        with self.db:
            row = self.db.execute("SELECT * FROM provider_execution_gates WHERE id=?", (gate_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown provider gate: {gate_id}")
            if row["status"] != "pending":
                raise ValueError(f"Provider gate {gate_id} is already {row['status']}")
            updated = self.db.execute("UPDATE provider_execution_gates SET status=?,decision_note=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (decision, note, gate_id))
            if updated.rowcount != 1:
                raise ValueError(f"Provider gate {gate_id} was decided concurrently")
            self.db.execute("DELETE FROM pending_provider_gate_claims WHERE gate_id=?", (gate_id,))
            self.db.execute(
                """INSERT OR IGNORE INTO pending_provider_gate_claims(provider,agent_id,task_id,gate_id)
                   SELECT provider,agent_id,task_id,MIN(id) FROM provider_execution_gates
                    WHERE provider=? AND agent_id=? AND task_id=? AND status='pending'
                    GROUP BY provider,agent_id,task_id""",
                (row["provider"], row["agent_id"], row["task_id"]),
            )
            self._event(f"provider.execution.{decision}", "provider_gate", gate_id, {"note": note, "approved_by": "Human"})

    def cancel_provider_execution(self, gate_id: int, note: str) -> None:
        """Revoke an unused pending or approved one-time execution gate."""
        with self.db:
            row = self.db.execute("SELECT * FROM provider_execution_gates WHERE id=?", (gate_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown provider gate: {gate_id}")
            if row["status"] not in {"pending", "approved"}:
                raise ValueError(f"Provider gate {gate_id} is already {row['status']}")
            updated = self.db.execute(
                "UPDATE provider_execution_gates SET status='rejected',decision_note=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('pending','approved')",
                (note, gate_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"Provider gate {gate_id} changed concurrently")
            self.db.execute("DELETE FROM pending_provider_gate_claims WHERE gate_id=?", (gate_id,))
            self._event(
                "provider.execution.cancelled",
                "provider_gate",
                gate_id,
                {"note": note, "approved_by": "Human", "previous_status": row["status"]},
            )

    def claim_provider_execution(self, gate_id: int, request_hash: str, definition_hash: str):
        from .worker_admission import registered_worker

        request_hash = _sha256_snapshot(request_hash, "request_hash")
        definition_hash = _sha256_snapshot(definition_hash, "definition_hash")
        mismatch_error: str | None = None
        attempt_id: int | None = None
        with self.db:
            self._begin_immediate()
            self._assert_dispatch_allowed()
            gate = self.db.execute(
                "SELECT agent_id,task_id FROM provider_execution_gates WHERE id=?", (gate_id,)
            ).fetchone()
            if not gate:
                raise KeyError(f"Unknown provider gate: {gate_id}")
            bound_project = self.db.execute(
                """SELECT 1 FROM worker_admission_projects p
                     JOIN work_items t ON t.project_id=p.project_id WHERE t.id=?""",
                (gate["task_id"],),
            ).fetchone()
            if registered_worker(self, str(gate["agent_id"])) or bound_project:
                raise PermissionError("Registered workers and projects require WorkerAdmissionService.admit")
            updated = self.db.execute(
                """UPDATE provider_execution_gates
                      SET status='claimed'
                    WHERE id=? AND status='approved'
                      AND request_hash=? AND definition_hash=?""",
                (gate_id, request_hash, definition_hash),
            )
            row = self.db.execute(
                "SELECT * FROM provider_execution_gates WHERE id=?", (gate_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown provider gate: {gate_id}")
            if updated.rowcount != 1:
                if row["status"] != "approved":
                    raise PermissionError(
                        f"Provider gate {gate_id} is {row['status']}, expected approved"
                    )
                note = (
                    "Approval snapshot no longer matches the current request or "
                    "provider policy; request a new gate."
                )
                invalidated = self.db.execute(
                    """UPDATE provider_execution_gates
                          SET status='rejected',
                              decision_note=CASE
                                  WHEN decision_note='' THEN ?
                                  ELSE decision_note || char(10) || ?
                              END,
                              decided_at=COALESCE(decided_at,CURRENT_TIMESTAMP)
                        WHERE id=? AND status='approved'""",
                    (note, note, gate_id),
                )
                if invalidated.rowcount != 1:
                    raise PermissionError(
                        f"Provider gate {gate_id} changed concurrently"
                    )
                self.db.execute(
                    "DELETE FROM pending_provider_gate_claims WHERE gate_id=?",
                    (gate_id,),
                )
                self._event(
                    "provider.execution.snapshot_mismatch",
                    "provider_gate",
                    gate_id,
                    {
                        "expected_request_hash": row["request_hash"],
                        "actual_request_hash": request_hash,
                        "expected_definition_hash": row["definition_hash"],
                        "actual_definition_hash": definition_hash,
                    },
                )
                mismatch_error = note
            else:
                cur = self.db.execute(
                    """INSERT INTO provider_execution_attempts(
                           identity,gate_id,provider,agent_id,task_id,request_hash,
                           definition_hash,status
                       ) VALUES(?,?,?,?,?,?,?,'claimed')""",
                    (
                        self._identity("provider-attempt"),
                        gate_id,
                        row["provider"],
                        row["agent_id"],
                        row["task_id"],
                        request_hash,
                        definition_hash,
                    ),
                )
                attempt_id = int(cur.lastrowid)
                assignment = self.db.execute(
                    """INSERT INTO assignments(
                           identity,task_id,agent_id,runtime,status
                       ) VALUES(?,?,?,?, 'active') RETURNING id""",
                    (
                        self._identity("assignment"),
                        row["task_id"],
                        row["agent_id"],
                        row["provider"],
                    ),
                ).fetchone()
                assignment_id = int(assignment["id"])
                self.db.execute(
                    """INSERT INTO worker_sessions(
                           identity,assignment_id,runtime,status
                       ) VALUES(?,?,?,'starting')""",
                    (self._identity("worker-session"), assignment_id, row["provider"]),
                )
                self.db.execute(
                    """INSERT INTO attempts(
                           identity,assignment_id,provider_attempt_id,ordinal,status
                       ) VALUES(?,?,?,?, 'claimed')""",
                    (self._identity("attempt"), assignment_id, attempt_id, 1),
                )
                self._event(
                    "provider.execution.claimed",
                    "provider_attempt",
                    attempt_id,
                    {
                        "gate_id": gate_id,
                        "request_hash": request_hash,
                        "definition_hash": definition_hash,
                    },
                )
        if mismatch_error is not None:
            raise PermissionError(mismatch_error)
        if attempt_id is None:
            raise RuntimeError("Provider gate claim did not create an attempt")
        return self.db.execute("SELECT * FROM provider_execution_attempts WHERE id=?", (attempt_id,)).fetchone()

    def consume_provider_execution(self, gate_id: int):
        """Compatibility alias: a caller must use claim_provider_execution with hashes."""
        raise RuntimeError("use claim_provider_execution(gate_id, request_hash, definition_hash)")

    def mark_provider_attempt_running(self, attempt_id: int, pid: int | None = None) -> None:
        with self.db:
            ensure_transition("attempt", "claimed", "running")
            updated = self.db.execute("UPDATE provider_execution_attempts SET status='running',version=version+1,pid=?,started_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP WHERE id=? AND status='claimed'", (pid, attempt_id))
            if updated.rowcount != 1: raise ValueError(f"Attempt {attempt_id} is not claimed")
            self.db.execute(
                """UPDATE attempts SET status='running',version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE provider_attempt_id=? AND status='claimed'""",
                (attempt_id,),
            )
            self.db.execute(
                """UPDATE worker_sessions SET status='running',version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE assignment_id=(SELECT assignment_id FROM attempts WHERE provider_attempt_id=?)
                      AND status='starting'""",
                (attempt_id,),
            )
            self._event("provider.execution.running", "provider_attempt", attempt_id, {"pid": pid})

    def finish_provider_attempt(self, attempt_id: int, status: str, result: str, metadata: dict[str, Any]) -> int:
        if status not in {"succeeded", "failed", "abandoned"}: raise ValueError(status)
        bounded_result = result[:100_000]
        with self.db:
            row = self.db.execute("SELECT * FROM provider_execution_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row: raise KeyError(f"Unknown attempt: {attempt_id}")
            if row["status"] not in {"claimed", "running"}: raise ValueError(f"Attempt {attempt_id} is already {row['status']}")
            ensure_transition("attempt", str(row["status"]), status)
            self.db.execute("UPDATE provider_execution_attempts SET status=?,version=version+1,result=?,metadata=?,finished_at=CURRENT_TIMESTAMP,heartbeat_at=CURRENT_TIMESTAMP WHERE id=?", (status, bounded_result, json.dumps(metadata), attempt_id))
            self.db.execute("UPDATE provider_execution_gates SET status='consumed',consumed_at=CURRENT_TIMESTAMP WHERE id=? AND status='claimed'", (row["gate_id"],))
            content = bounded_result if status == "succeeded" else ""
            cur = self.db.execute(
                """INSERT INTO provider_execution_artifacts(
                       identity,gate_id,attempt_id,provider,agent_id,content,
                       metadata,status,digest,producer_json,verifier_json,
                       inputs_json,toolchain_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("provider-artifact"),
                    row["gate_id"],
                    attempt_id,
                    row["provider"],
                    row["agent_id"],
                    content,
                    json.dumps(metadata),
                    status,
                    self._content_digest(content),
                    json.dumps(
                        {"agent_id": row["agent_id"], "provider": row["provider"]},
                        sort_keys=True,
                    ),
                    json.dumps({"status": "unverified"}, sort_keys=True),
                    json.dumps(
                        {"gate_id": row["gate_id"], "attempt_id": attempt_id},
                        sort_keys=True,
                    ),
                    json.dumps(
                        {"provider": row["provider"], "metadata": metadata},
                        sort_keys=True,
                    ),
                ),
            )
            artifact_id = int(cur.lastrowid)
            assignment_status = "cancelled" if status == "abandoned" else status
            session_status = "cancelled" if status == "abandoned" else status
            self.db.execute(
                """UPDATE attempts SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE provider_attempt_id=? AND status IN ('claimed','running')""",
                (status, attempt_id),
            )
            self.db.execute(
                """UPDATE assignments SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=(SELECT assignment_id FROM attempts WHERE provider_attempt_id=?)
                      AND status='active'""",
                (assignment_status, attempt_id),
            )
            self.db.execute(
                """UPDATE worker_sessions SET status='running',version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE assignment_id=(SELECT assignment_id FROM attempts WHERE provider_attempt_id=?)
                      AND status='starting' AND ?='succeeded'""",
                (attempt_id, status),
            )
            self.db.execute(
                """UPDATE worker_sessions SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP
                    WHERE assignment_id=(SELECT assignment_id FROM attempts WHERE provider_attempt_id=?)
                      AND status IN ('starting','running')""",
                (session_status, attempt_id),
            )
            self._event(f"provider.execution.{status}", "provider_attempt", attempt_id, {"artifact_id": artifact_id})
        return artifact_id

    def reconcile_provider_attempts(self) -> list[int]:
        rows = self.db.execute("SELECT id FROM provider_execution_attempts WHERE status IN ('claimed','running')").fetchall()
        reconciled = []
        for row in rows:
            attempt_id = int(row["id"])
            self.finish_provider_attempt(attempt_id, "abandoned", "Interrupted before a durable terminal result", {"reconciled": True, "retry_requires_new_gate": True})
            reconciled.append(attempt_id)
        return reconciled

    def add_provider_artifact(self, gate_id: int, provider: str, agent_id: str, content: str, metadata: dict[str, Any]) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO provider_execution_artifacts(
                       identity,gate_id,provider,agent_id,content,metadata,digest,
                       producer_json,verifier_json,inputs_json,toolchain_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._identity("provider-artifact"),
                    gate_id,
                    provider,
                    agent_id,
                    content,
                    json.dumps(metadata),
                    self._content_digest(content),
                    json.dumps(
                        {"agent_id": agent_id, "provider": provider}, sort_keys=True
                    ),
                    json.dumps({"status": "unverified"}, sort_keys=True),
                    json.dumps({"gate_id": gate_id, "attempt_id": None}, sort_keys=True),
                    json.dumps(
                        {"provider": provider, "metadata": metadata}, sort_keys=True
                    ),
                ),
            )
            artifact_id = int(cur.lastrowid)
            self._event("provider.artifact.created", "provider_artifact", artifact_id, {"gate_id": gate_id, "provider": provider, "agent_id": agent_id})
        return artifact_id

    def provider_artifacts(self):
        return self.db.execute("SELECT * FROM provider_execution_artifacts ORDER BY id").fetchall()

    def create_github_plan(self, repo: str, operations: list[dict[str, Any]]) -> tuple[int, str]:
        import hashlib
        if not repo or "/" not in repo:
            raise ValueError("GitHub plan requires owner/repository")
        keys = [str(op.get("idempotency_key", "")) for op in operations]
        if not operations or any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("Operations require unique non-empty idempotency keys")
        document = {"version": 1, "repo": repo, "operations": operations}
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        plan_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.db:
            existing = self.db.execute("SELECT id FROM github_mutation_plans WHERE plan_hash=?", (plan_hash,)).fetchone()
            if existing:
                return int(existing["id"]), plan_hash
            cur = self.db.execute("INSERT INTO github_mutation_plans(repo,plan_json,plan_hash) VALUES(?,?,?)", (repo, payload, plan_hash))
            plan_id = int(cur.lastrowid)
            self._event("github.plan.created", "github_plan", plan_id, {"repo": repo, "plan_hash": plan_hash, "operation_count": len(operations)})
        return plan_id, plan_hash

    def github_plan(self, plan_id: int):
        row = self.db.execute("SELECT * FROM github_mutation_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown GitHub plan: {plan_id}")
        return row

    def request_github_gate(self, plan_id: int) -> int:
        plan = self.github_plan(plan_id)
        with self.db:
            cur = self.db.execute("INSERT INTO github_mutation_gates(plan_id,repo,plan_hash) VALUES(?,?,?)", (plan_id, plan["repo"], plan["plan_hash"]))
            gate_id = int(cur.lastrowid)
            self._event("github.gate.requested", "github_gate", gate_id, {"plan_id": plan_id, "repo": plan["repo"], "plan_hash": plan["plan_hash"]})
        return gate_id

    def github_gates(self):
        return self.db.execute("SELECT * FROM github_mutation_gates ORDER BY id").fetchall()

    def decide_github_gate(self, gate_id: int, decision: str, note: str) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError(decision)
        with self.db:
            updated = self.db.execute("UPDATE github_mutation_gates SET status=?,decision_note=?,decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", (decision, note, gate_id))
            if updated.rowcount != 1:
                row = self.db.execute("SELECT status FROM github_mutation_gates WHERE id=?", (gate_id,)).fetchone()
                if not row: raise KeyError(f"Unknown GitHub gate: {gate_id}")
                raise ValueError(f"GitHub gate {gate_id} is already {row['status']}")
            self._event(f"github.gate.{decision}", "github_gate", gate_id, {"note": note, "approved_by": "Human"})

    def claim_github_gate(self, gate_id: int, plan_id: int, repo: str, plan_hash: str):
        with self.db:
            row = self.db.execute("SELECT * FROM github_mutation_gates WHERE id=?", (gate_id,)).fetchone()
            if not row: raise KeyError(f"Unknown GitHub gate: {gate_id}")
            if (int(row["plan_id"]), row["repo"], row["plan_hash"]) != (plan_id, repo, plan_hash):
                raise PermissionError("Approval gate does not match plan repository and SHA-256")
            updated = self.db.execute("UPDATE github_mutation_gates SET status='claimed' WHERE id=? AND status='approved'", (gate_id,))
            if updated.rowcount != 1: raise PermissionError(f"GitHub gate {gate_id} is {row['status']}, expected approved")
            self._event("github.gate.claimed", "github_gate", gate_id, {"plan_id": plan_id, "repo": repo, "plan_hash": plan_hash})
        return row

    def github_completed_keys(self, repo: str) -> set[str]:
        return {str(row[0]) for row in self.db.execute("SELECT idempotency_key FROM github_idempotency WHERE repo=?", (repo,))}

    def finish_github_apply(self, gate_id: int, plan_id: int, report: dict[str, Any]) -> int:
        results = list(report.get("results", []))
        failures = [r for r in results if not r.get("ok") and not r.get("skipped")]
        successes = [r for r in results if r.get("ok")]
        status = "partial" if failures and successes else ("failed" if failures or not report.get("ok", False) else "succeeded")
        with self.db:
            gate = self.db.execute("SELECT * FROM github_mutation_gates WHERE id=?", (gate_id,)).fetchone()
            if not gate or gate["status"] != "claimed" or int(gate["plan_id"]) != plan_id:
                raise PermissionError("GitHub gate is not claimed for this plan")
            for result in successes:
                self.db.execute("INSERT OR IGNORE INTO github_idempotency(repo,idempotency_key,result_json) VALUES(?,?,?)", (gate["repo"], result["idempotency_key"], json.dumps(result, sort_keys=True)))
            cur = self.db.execute("INSERT INTO github_mutation_reports(gate_id,plan_id,status,report_json) VALUES(?,?,?,?)", (gate_id, plan_id, status, json.dumps(report, sort_keys=True)))
            report_id = int(cur.lastrowid)
            self.db.execute("UPDATE github_mutation_gates SET status='consumed',consumed_at=CURRENT_TIMESTAMP WHERE id=? AND status='claimed'", (gate_id,))
            self._event(f"github.apply.{status}", "github_report", report_id, {"gate_id": gate_id, "plan_id": plan_id, "successes": len(successes), "failures": len(failures)})
        return report_id

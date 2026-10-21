"""Schema 6: immutable human judgments, separate from observed production facts."""

MIGRATION = """
BEGIN;
CREATE TABLE annotation_targets (
    annotation_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('evaluation','opportunity')),
    opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
    evaluation_id TEXT REFERENCES evaluations ON DELETE CASCADE,
    created_at REAL NOT NULL,
    CHECK((target_type='evaluation' AND evaluation_id IS NOT NULL)
       OR (target_type='opportunity' AND evaluation_id IS NULL)));
CREATE UNIQUE INDEX annotation_evaluation_target ON annotation_targets(evaluation_id)
    WHERE target_type='evaluation';
CREATE UNIQUE INDEX annotation_opportunity_target ON annotation_targets(opportunity_id)
    WHERE target_type='opportunity';
CREATE INDEX annotation_parent ON annotation_targets(opportunity_id);
CREATE TABLE annotation_revisions (
    annotation_id TEXT NOT NULL REFERENCES annotation_targets ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision>=1),
    label TEXT NOT NULL CHECK(label IN ('should_reply','should_not_reply','uncertain')),
    reason TEXT CHECK(reason IS NULL OR length(reason)<=2000),
    actor_id TEXT NOT NULL CHECK(actor_id='webui:shared-admin'),
    revision_created_at REAL NOT NULL,
    context_quality TEXT NOT NULL CHECK(context_quality IN
        ('identity_context_available','partial_context','critical_context_missing')),
    declared_context_quality TEXT NOT NULL,
    evidence TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    PRIMARY KEY(annotation_id,revision));
CREATE TRIGGER annotation_parent_check BEFORE INSERT ON annotation_targets
WHEN NEW.target_type='evaluation' AND NOT EXISTS(
    SELECT 1 FROM evaluations WHERE evaluation_id=NEW.evaluation_id
    AND opportunity_id=NEW.opportunity_id)
BEGIN SELECT RAISE(ABORT,'annotation target mismatch'); END;
CREATE TRIGGER annotation_target_immutable BEFORE UPDATE ON annotation_targets
BEGIN SELECT RAISE(ABORT,'annotation target is immutable'); END;
CREATE TRIGGER annotation_target_no_direct_delete BEFORE DELETE ON annotation_targets
WHEN EXISTS(SELECT 1 FROM opportunities WHERE opportunity_id=OLD.opportunity_id)
 AND (OLD.target_type='opportunity' OR EXISTS(
    SELECT 1 FROM evaluations WHERE evaluation_id=OLD.evaluation_id))
BEGIN SELECT RAISE(ABORT,'delete the observation parent instead'); END;
CREATE TRIGGER annotation_revision_immutable BEFORE UPDATE ON annotation_revisions
BEGIN SELECT RAISE(ABORT,'annotation revision is immutable'); END;
CREATE TRIGGER annotation_revision_no_direct_delete BEFORE DELETE ON annotation_revisions
WHEN EXISTS(SELECT 1 FROM annotation_targets WHERE annotation_id=OLD.annotation_id)
BEGIN SELECT RAISE(ABORT,'annotation revision is immutable'); END;
CREATE TRIGGER annotation_revision_sequence BEFORE INSERT ON annotation_revisions
WHEN NEW.revision!=COALESCE((SELECT MAX(revision)+1 FROM annotation_revisions
    WHERE annotation_id=NEW.annotation_id),1)
BEGIN SELECT RAISE(ABORT,'annotation revision sequence conflict'); END;
PRAGMA user_version=6;
COMMIT;
"""

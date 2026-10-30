"""Store immutable candidates, submitted intent and accepted history."""

from __future__ import annotations

DECISION_SQL = r"""
CREATE TABLE memory_candidate (
  id TEXT PRIMARY KEY NOT NULL,
  request_id TEXT NOT NULL REFERENCES review_request(id),
  payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json)),
  digest TEXT NOT NULL,
  normalization_version INTEGER NOT NULL CHECK(normalization_version=1),
  enrichment_version INTEGER NOT NULL CHECK(enrichment_version=1),
  origin_unresolved_id TEXT REFERENCES unresolved(id),
  state TEXT NOT NULL CHECK(state IN
    ('waiting_verification','approved_pending','saved','rejected','blocked',
     'superseded','erased')),
  revision INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(revision)='integer' AND revision>=0),
  active_decision_id TEXT REFERENCES memory_decision(id),
  application_request_id TEXT REFERENCES review_request(id),
  derived_digest TEXT, code TEXT, created_at REAL NOT NULL,
  UNIQUE(id,digest),
  CHECK((state='erased' AND payload_json IS NULL) OR
    (state!='erased' AND payload_json IS NOT NULL)));
CREATE INDEX memory_candidate_request ON memory_candidate(request_id);
CREATE INDEX memory_candidate_unresolved
  ON memory_candidate(origin_unresolved_id);
CREATE TRIGGER memory_candidate_immutable BEFORE UPDATE ON memory_candidate
  WHEN new.id IS NOT old.id OR new.request_id IS NOT old.request_id
    OR new.digest IS NOT old.digest
    OR new.normalization_version IS NOT old.normalization_version
    OR new.enrichment_version IS NOT old.enrichment_version
    OR new.origin_unresolved_id IS NOT old.origin_unresolved_id
    OR new.created_at IS NOT old.created_at
    OR (new.payload_json IS NOT old.payload_json AND
      NOT(old.payload_json IS NOT NULL AND new.payload_json IS NULL
        AND new.state='erased'))
    OR new.revision<old.revision
    OR (old.state='erased' AND new.state!='erased') BEGIN
  SELECT RAISE(ABORT, 'memory candidate is immutable'); END;
CREATE TABLE memory_decision (
  id TEXT PRIMARY KEY NOT NULL,
  candidate_id TEXT NOT NULL REFERENCES memory_candidate(id),
  candidate_digest TEXT NOT NULL, intent_key TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL CHECK(action IN ('approve','reject','edit')),
  replacement_candidate_id TEXT REFERENCES memory_candidate(id),
  locator_json TEXT CHECK(locator_json IS NULL OR json_valid(locator_json)),
  submitted_at REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('waiting_verification','verified','blocked','erased')),
  accepted_revision INTEGER CHECK(accepted_revision IS NULL OR
    (typeof(accepted_revision)='integer' AND accepted_revision>0)),
  owner_evidence_id TEXT REFERENCES review_evidence(id),
  display_evidence_id TEXT REFERENCES review_evidence(id),
  accepted_at REAL, code TEXT,
  UNIQUE(candidate_id,accepted_revision),
  FOREIGN KEY(candidate_id,candidate_digest)
    REFERENCES memory_candidate(id,digest),
  CHECK(accepted_revision IS NULL OR
    (accepted_at IS NOT NULL AND (state='erased' OR
      (state='verified' AND owner_evidence_id IS NOT NULL
        AND display_evidence_id IS NOT NULL)))),
  CHECK(accepted_revision IS NOT NULL OR
    (owner_evidence_id IS NULL AND display_evidence_id IS NULL
      AND accepted_at IS NULL)),
  CHECK(state!='erased' OR (locator_json IS NULL
    AND owner_evidence_id IS NULL AND display_evidence_id IS NULL)));
CREATE INDEX memory_decision_candidate ON memory_decision(candidate_id);
CREATE TRIGGER memory_decision_immutable BEFORE UPDATE ON memory_decision
  WHEN new.id IS NOT old.id OR new.candidate_id IS NOT old.candidate_id
    OR new.candidate_digest IS NOT old.candidate_digest
    OR new.intent_key IS NOT old.intent_key OR new.action IS NOT old.action
    OR new.replacement_candidate_id IS NOT old.replacement_candidate_id
    OR new.submitted_at IS NOT old.submitted_at
    OR (new.locator_json IS NOT old.locator_json AND
      NOT(new.locator_json IS NULL AND new.state='erased'))
    OR (old.accepted_revision IS NOT NULL AND
      (new.accepted_revision IS NOT old.accepted_revision
        OR new.accepted_at IS NOT old.accepted_at
        OR (new.owner_evidence_id IS NOT old.owner_evidence_id AND
          NOT(new.owner_evidence_id IS NULL AND new.state='erased'))
        OR (new.display_evidence_id IS NOT old.display_evidence_id AND
          NOT(new.display_evidence_id IS NULL AND new.state='erased'))
        OR new.state NOT IN ('verified','erased')))
    OR (old.state='erased' AND new.state!='erased') BEGIN
  SELECT RAISE(ABORT, 'memory decision is immutable'); END;
CREATE TRIGGER memory_decision_history BEFORE DELETE ON memory_decision
  WHEN old.accepted_revision IS NOT NULL BEGIN
  SELECT RAISE(ABORT, 'memory decision history is immutable'); END;
CREATE TABLE memory_candidate_support (
  candidate_id TEXT NOT NULL REFERENCES memory_candidate(id),
  evidence_id TEXT NOT NULL REFERENCES review_evidence(id),
  role TEXT NOT NULL CHECK(role IN ('original','display','owner')),
  PRIMARY KEY(candidate_id,evidence_id,role));
CREATE INDEX memory_candidate_support_evidence
  ON memory_candidate_support(evidence_id);
CREATE TRIGGER review_request_application_immutable
  BEFORE UPDATE OF candidate_id,decision_revision ON review_request
  WHEN new.candidate_id IS NOT old.candidate_id
    OR new.decision_revision IS NOT old.decision_revision BEGIN
  SELECT RAISE(ABORT, 'review application is immutable'); END;
CREATE TRIGGER review_request_application_accepted
  BEFORE INSERT ON review_request
  WHEN new.candidate_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM memory_decision d WHERE d.candidate_id=new.candidate_id
      AND d.accepted_revision=new.decision_revision
      AND d.action='approve') BEGIN
  SELECT RAISE(ABORT, 'review application needs accepted approval'); END;
"""

"""Durable review and revisioned knowledge tables for marked schema 1."""

from __future__ import annotations

from muninn.review_state import STATE_SQL

KNOWLEDGE_SQL = r"""
CREATE TABLE store_meta (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1),
  format TEXT NOT NULL CHECK(format='muninn-knowledge'),
  version INTEGER NOT NULL CHECK(version=1));
INSERT INTO store_meta VALUES (1,'muninn-knowledge',1);
CREATE TABLE review_control (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton=1),
  generation INTEGER NOT NULL CHECK(generation>=0),
  admission INTEGER NOT NULL CHECK(admission IN (0,1)),
  policy_json TEXT CHECK(policy_json IS NULL OR json_valid(policy_json)),
  policy_digest TEXT, activated_at REAL);
INSERT INTO review_control VALUES (1,0,0,NULL,NULL,NULL);
CREATE TABLE review_source (
  provider TEXT NOT NULL, thread_id TEXT NOT NULL,
  activation_line INTEGER NOT NULL CHECK(activation_line>=0),
  examined_line INTEGER NOT NULL CHECK(examined_line>=0),
  eligible_line INTEGER NOT NULL CHECK(eligible_line>=0),
  completed_line INTEGER NOT NULL CHECK(completed_line>=0),
  boundary_hash TEXT, updated_at REAL NOT NULL,
  PRIMARY KEY(provider,thread_id));
CREATE TABLE review_request (
  id TEXT PRIMARY KEY NOT NULL, duplicate_key TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL, thread_id TEXT NOT NULL,
  session_root TEXT NOT NULL,
  from_line INTEGER NOT NULL CHECK(from_line>0),
  through_line INTEGER NOT NULL CHECK(through_line>=from_line),
  boundary_hash TEXT NOT NULL,
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
  policy_digest TEXT NOT NULL, trigger TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('review','proposal')),
  state TEXT NOT NULL CHECK(state IN
    ('pending','running','waiting','completed','failed','cancelled')),
  current_attempt_id TEXT, created_at REAL NOT NULL,
  retry_at REAL, cancel_code TEXT,
  UNIQUE(provider,thread_id,from_line,through_line,boundary_hash,
    scope_json,policy_digest));
CREATE TRIGGER review_request_policy_immutable
  BEFORE UPDATE OF policy_json,policy_digest ON review_request
  WHEN new.policy_json IS NOT old.policy_json
    OR new.policy_digest IS NOT old.policy_digest BEGIN
  SELECT RAISE(ABORT, 'review policy is immutable'); END;
CREATE INDEX review_request_ready
  ON review_request(state,retry_at,created_at);
CREATE INDEX review_request_source
  ON review_request(provider,thread_id,from_line,through_line);
CREATE TABLE review_attempt (
  id TEXT PRIMARY KEY NOT NULL,
  request_id TEXT NOT NULL REFERENCES review_request(id),
  generation INTEGER NOT NULL,
  requested_json TEXT
    CHECK(requested_json IS NULL OR json_valid(requested_json)),
  observed_json TEXT NOT NULL CHECK(json_valid(observed_json)),
  started_at REAL NOT NULL, progress_at REAL NOT NULL,
  ended_at REAL, outcome TEXT, error_code TEXT,
  usage_json TEXT CHECK(usage_json IS NULL OR json_valid(usage_json)));
CREATE INDEX review_attempt_request
  ON review_attempt(request_id,started_at);
CREATE TABLE review_call (
  attempt_id TEXT NOT NULL REFERENCES review_attempt(id),
  round_index INTEGER NOT NULL CHECK(round_index>0),
  budget_account_id TEXT NOT NULL, policy_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('reserved','launched','completed','unknown')),
  reserved_at REAL NOT NULL, launched_at REAL, ended_at REAL,
  requested_json TEXT NOT NULL CHECK(json_valid(requested_json)),
  observed_json TEXT NOT NULL CHECK(json_valid(observed_json)),
  usage_json TEXT CHECK(usage_json IS NULL OR json_valid(usage_json)),
  cost_reservation_decimal TEXT, currency TEXT,
  process_identity_json TEXT CHECK(process_identity_json IS NULL
    OR json_valid(process_identity_json)),
  outcome TEXT, PRIMARY KEY(attempt_id,round_index));
CREATE INDEX review_call_budget
  ON review_call(budget_account_id,policy_digest,reserved_at);
CREATE TABLE review_evidence (
  id TEXT PRIMARY KEY NOT NULL,
  provider TEXT NOT NULL, thread_id TEXT NOT NULL,
  session_root TEXT NOT NULL,
  line INTEGER NOT NULL CHECK(line>0),
  part INTEGER NOT NULL CHECK(part>0),
  line_sha256 TEXT NOT NULL, source_version TEXT NOT NULL,
  projection_version TEXT NOT NULL,
  original_locator_json TEXT NOT NULL CHECK(json_valid(original_locator_json)),
  selector_json TEXT NOT NULL CHECK(json_valid(selector_json)),
  selector_digest TEXT NOT NULL, evidence_type TEXT NOT NULL,
  authority TEXT NOT NULL,
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  excerpt TEXT,
  state TEXT NOT NULL CHECK(state IN ('live','changed','erased')),
  selected_at REAL NOT NULL,
  UNIQUE(provider,thread_id,line,part,line_sha256,
    projection_version,selector_digest));
CREATE INDEX review_evidence_source
  ON review_evidence(provider,thread_id,line,part);
CREATE TABLE review_input (
  request_id TEXT NOT NULL REFERENCES review_request(id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES review_evidence(id),
  PRIMARY KEY(request_id,evidence_id));
CREATE INDEX review_input_evidence ON review_input(evidence_id,request_id);
CREATE TABLE knowledge (
  id TEXT PRIMARY KEY NOT NULL, current_revision INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('active','withheld','retracted','erased')),
  retracted_at REAL, retracted_reason TEXT);
CREATE TABLE knowledge_version (
  knowledge_id TEXT NOT NULL REFERENCES knowledge(id),
  revision INTEGER NOT NULL,
  request_id TEXT NOT NULL REFERENCES review_request(id),
  form TEXT NOT NULL CHECK(form IN ('note','guide')),
  kind TEXT NOT NULL CHECK(kind IN
    ('decision','fact','preference','procedure','lesson','constraint')),
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  content_json TEXT CHECK(content_json IS NULL OR json_valid(content_json)),
  reason TEXT,
  origin TEXT NOT NULL CHECK(origin IN ('review','proposal')),
  expiry_json TEXT CHECK(expiry_json IS NULL OR json_valid(expiry_json)),
  created_at REAL NOT NULL, erased_at REAL,
  PRIMARY KEY(knowledge_id,revision));
CREATE INDEX knowledge_version_request ON knowledge_version(request_id);
CREATE TABLE knowledge_claim (
  knowledge_id TEXT NOT NULL, revision INTEGER NOT NULL,
  claim_key TEXT NOT NULL, text TEXT,
  dependency_keys_json TEXT NOT NULL CHECK(json_valid(dependency_keys_json)),
  state TEXT NOT NULL CHECK(state IN ('live','withheld','erased')),
  PRIMARY KEY(knowledge_id,revision,claim_key),
  FOREIGN KEY(knowledge_id,revision)
    REFERENCES knowledge_version(knowledge_id,revision));
CREATE TABLE knowledge_support (
  knowledge_id TEXT NOT NULL, revision INTEGER NOT NULL,
  claim_key TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES review_evidence(id),
  PRIMARY KEY(knowledge_id,revision,claim_key,evidence_id),
  FOREIGN KEY(knowledge_id,revision,claim_key)
    REFERENCES knowledge_claim(knowledge_id,revision,claim_key)
    ON DELETE CASCADE);
CREATE INDEX knowledge_support_evidence ON knowledge_support(evidence_id);
CREATE TABLE unresolved (
  id TEXT PRIMARY KEY NOT NULL,
  request_id TEXT NOT NULL REFERENCES review_request(id),
  item_json TEXT CHECK(item_json IS NULL OR json_valid(item_json)),
  affected_json TEXT NOT NULL CHECK(json_valid(affected_json)),
  support_json TEXT NOT NULL CHECK(json_valid(support_json)),
  created_at REAL NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','resolved','erased')));
CREATE INDEX unresolved_request ON unresolved(request_id,state);
CREATE TABLE review_receipt (
  request_id TEXT PRIMARY KEY NOT NULL REFERENCES review_request(id),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES review_attempt(id),
  applied_json TEXT NOT NULL CHECK(json_valid(applied_json)),
  unresolved_json TEXT NOT NULL CHECK(json_valid(unresolved_json)),
  committed_at REAL NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN
    ('completed','completed_unresolved','no_change')),
  no_change_reason TEXT);
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
  knowledge_id UNINDEXED, revision UNINDEXED, text);
INSERT INTO knowledge_fts(knowledge_fts, rank) VALUES ('secure-delete', 1);
""" + STATE_SQL

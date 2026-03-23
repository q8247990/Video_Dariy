"""postgresql baseline schema snapshot

Revision ID: 20260320_0001
Revises:
Create Date: 2026-03-20 00:00:00.000000
"""

# ruff: noqa: E501

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260320_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BASELINE_SQL = """
CREATE TABLE admin_user (
	id SERIAL NOT NULL,
	username VARCHAR(64) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	password_salt VARCHAR(255),
	last_login_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (username)
);

CREATE TABLE app_runtime_state (
	id SERIAL NOT NULL,
	state_key VARCHAR(128) NOT NULL,
	state_value JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (state_key)
);

CREATE TABLE home_entity_profile (
	id SERIAL NOT NULL,
	entity_type VARCHAR(32) NOT NULL,
	name VARCHAR(128) NOT NULL,
	role_type VARCHAR(64) NOT NULL,
	age_group VARCHAR(32),
	breed VARCHAR(128),
	appearance_desc TEXT,
	personality_desc TEXT,
	note TEXT,
	sort_order INTEGER NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_home_entity_profile_is_enabled ON home_entity_profile (is_enabled);
CREATE INDEX ix_home_entity_profile_entity_type ON home_entity_profile (entity_type);
CREATE INDEX ix_home_entity_profile_sort_order ON home_entity_profile (sort_order);

CREATE TABLE home_profile (
	id SERIAL NOT NULL,
	home_name VARCHAR(128) NOT NULL,
	family_tags_json JSON,
	focus_points_json JSON,
	system_style VARCHAR(32) NOT NULL,
	style_preference_text TEXT,
	assistant_name VARCHAR(128) NOT NULL,
	home_note TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE llm_provider (
	id SERIAL NOT NULL,
	provider_name VARCHAR(128) NOT NULL,
	provider_type VARCHAR(32) NOT NULL,
	api_base_url VARCHAR(512) NOT NULL,
	api_key_encrypted TEXT NOT NULL,
	model_name VARCHAR(128) NOT NULL,
	timeout_seconds INTEGER NOT NULL,
	retry_count INTEGER NOT NULL,
	extra_config_json JSON,
	enabled BOOLEAN NOT NULL,
	is_default BOOLEAN NOT NULL,
	supports_vision BOOLEAN NOT NULL,
	supports_qa BOOLEAN NOT NULL,
	is_default_vision BOOLEAN NOT NULL,
	is_default_qa BOOLEAN NOT NULL,
	last_test_status VARCHAR(32),
	last_test_message VARCHAR(512),
	last_test_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX idx_llm_provider_type_default ON llm_provider (provider_type, is_default);
CREATE INDEX idx_llm_provider_qa_default ON llm_provider (supports_qa, is_default_qa, enabled);
CREATE INDEX idx_llm_provider_type_enabled ON llm_provider (provider_type, enabled);
CREATE INDEX idx_llm_provider_vision_default ON llm_provider (supports_vision, is_default_vision, enabled);

CREATE TABLE mcp_call_log (
	id SERIAL NOT NULL,
	tool_name VARCHAR(128) NOT NULL,
	request_json JSON,
	response_json JSON,
	status VARCHAR(32) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX idx_mcp_call_log_tool_created ON mcp_call_log (tool_name, created_at);

CREATE TABLE system_config (
	id SERIAL NOT NULL,
	config_key VARCHAR(128) NOT NULL,
	config_value JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (config_key)
);

CREATE TABLE tag_definition (
	id SERIAL NOT NULL,
	tag_name VARCHAR(128) NOT NULL,
	tag_type VARCHAR(32) NOT NULL,
	description VARCHAR(512),
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX uk_tag_definition_name_type ON tag_definition (tag_name, tag_type);
CREATE INDEX idx_tag_definition_enabled ON tag_definition (enabled);

CREATE TABLE task_log (
	id SERIAL NOT NULL,
	task_type VARCHAR(64) NOT NULL,
	task_target_id BIGINT,
	dedupe_key VARCHAR(255),
	status VARCHAR(32) NOT NULL,
	queue_task_id VARCHAR(128),
	cancel_requested BOOLEAN NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	retry_count INTEGER NOT NULL,
	message VARCHAR(512),
	detail_json JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_task_log_queue_task_id ON task_log (queue_task_id);
CREATE INDEX idx_task_log_type_target_status ON task_log (task_type, task_target_id, status);
CREATE INDEX idx_task_log_target_type_status_created ON task_log (task_target_id, task_type, status, created_at);
CREATE INDEX idx_task_log_created_at ON task_log (created_at);
CREATE INDEX idx_task_log_status_type ON task_log (status, task_type);

CREATE TABLE video_source (
	id SERIAL NOT NULL,
	source_name VARCHAR(128) NOT NULL,
	camera_name VARCHAR(128) NOT NULL,
	location_name VARCHAR(255) NOT NULL,
	description TEXT,
	prompt_text TEXT,
	source_type VARCHAR(32) NOT NULL,
	config_json JSON,
	enabled BOOLEAN NOT NULL,
	source_paused BOOLEAN NOT NULL,
	paused_at TIMESTAMP WITHOUT TIME ZONE,
	analyze_from_date DATE,
	backfill_start_date DATE,
	last_scan_at TIMESTAMP WITHOUT TIME ZONE,
	last_validate_status VARCHAR(32),
	last_validate_message VARCHAR(512),
	last_validate_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_video_source_enabled ON video_source (enabled);

CREATE TABLE webhook_config (
	id SERIAL NOT NULL,
	name VARCHAR(128) NOT NULL,
	url VARCHAR(1024) NOT NULL,
	headers_json JSON,
	event_types_json JSON,
	event_subscriptions_json JSON,
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE chat_query_log (
	id SERIAL NOT NULL,
	user_question TEXT NOT NULL,
	parsed_condition_json JSON,
	answer_text TEXT NOT NULL,
	referenced_event_ids_json JSON,
	provider_id INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(provider_id) REFERENCES llm_provider (id)
);
CREATE INDEX idx_chat_query_log_created_at ON chat_query_log (created_at);

CREATE TABLE daily_summary (
	id SERIAL NOT NULL,
	summary_date DATE NOT NULL,
	summary_title VARCHAR(128),
	overall_summary TEXT,
	subject_sections_json JSON,
	attention_items_json JSON,
	event_count INTEGER NOT NULL,
	provider_id INTEGER,
	generated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (summary_date),
	FOREIGN KEY(provider_id) REFERENCES llm_provider (id)
);

CREATE TABLE llm_usage_log (
	id SERIAL NOT NULL,
	provider_id INTEGER NOT NULL,
	usage_date DATE NOT NULL,
	scene VARCHAR(64) NOT NULL,
	prompt_tokens INTEGER NOT NULL,
	completion_tokens INTEGER NOT NULL,
	total_tokens INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(provider_id) REFERENCES llm_provider (id)
);
CREATE INDEX idx_llm_usage_log_provider_date ON llm_usage_log (provider_id, usage_date);
CREATE INDEX idx_llm_usage_log_date ON llm_usage_log (usage_date);

CREATE TABLE video_file (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	file_name VARCHAR(255) NOT NULL,
	file_path VARCHAR(1024) NOT NULL,
	file_path_hash VARCHAR(64) NOT NULL,
	storage_type VARCHAR(32) NOT NULL,
	access_uri VARCHAR(1024),
	file_format VARCHAR(32),
	file_size BIGINT,
	start_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	end_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	duration_seconds INTEGER,
	file_hash VARCHAR(128),
	parse_status VARCHAR(32) NOT NULL,
	parse_message VARCHAR(512),
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES video_source (id)
);
CREATE INDEX idx_video_file_parse_status ON video_file (parse_status);
CREATE UNIQUE INDEX uk_video_file_source_path_hash ON video_file (source_id, file_path_hash);
CREATE INDEX idx_video_file_source_start ON video_file (source_id, start_time);

CREATE TABLE video_session (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	session_start_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	session_end_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	total_duration_seconds INTEGER,
	merge_rule VARCHAR(128),
	analysis_status VARCHAR(32) NOT NULL,
	summary_text TEXT,
	activity_level VARCHAR(16),
	main_subjects_json JSON,
	has_important_event BOOLEAN,
	analysis_notes_json JSON,
	last_analyzed_at TIMESTAMP WITHOUT TIME ZONE,
	analysis_priority VARCHAR(16),
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES video_source (id)
);
CREATE INDEX idx_video_session_source_start ON video_session (source_id, session_start_time);
CREATE INDEX idx_video_session_source_status_end ON video_session (source_id, analysis_status, session_end_time);
CREATE INDEX idx_video_session_status ON video_session (analysis_status);
CREATE INDEX idx_video_session_priority ON video_session (analysis_priority, analysis_status);

CREATE TABLE video_source_runtime_state (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	backfill_paused BOOLEAN NOT NULL,
	backfill_cursor_time TIMESTAMP WITHOUT TIME ZONE,
	backfill_last_dispatch_at TIMESTAMP WITHOUT TIME ZONE,
	latency_alert_counter INTEGER NOT NULL,
	latency_alert_active BOOLEAN NOT NULL,
	latency_alert_last_notified_at TIMESTAMP WITHOUT TIME ZONE,
	backfill_stuck_alert_counter INTEGER NOT NULL,
	backfill_stuck_alert_active BOOLEAN NOT NULL,
	backfill_stuck_alert_last_notified_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_id),
	FOREIGN KEY(source_id) REFERENCES video_source (id)
);
CREATE UNIQUE INDEX idx_video_source_runtime_state_source ON video_source_runtime_state (source_id);

CREATE TABLE event_record (
	id SERIAL NOT NULL,
	source_id INTEGER NOT NULL,
	session_id INTEGER NOT NULL,
	event_start_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	event_end_time TIMESTAMP WITHOUT TIME ZONE,
	object_type VARCHAR(32),
	action_type VARCHAR(64),
	description TEXT NOT NULL,
	confidence_score DECIMAL(5, 4),
	event_type VARCHAR(64),
	title VARCHAR(255),
	summary TEXT,
	detail TEXT,
	importance_level VARCHAR(16),
	offset_start_sec DECIMAL(10, 3),
	offset_end_sec DECIMAL(10, 3),
	related_entities_json JSON,
	observed_actions_json JSON,
	interpreted_state_json JSON,
	raw_result JSON,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES video_source (id),
	FOREIGN KEY(session_id) REFERENCES video_session (id)
);
CREATE INDEX idx_event_session_id ON event_record (session_id);
CREATE INDEX idx_event_object_type ON event_record (object_type);
CREATE INDEX ix_event_record_importance_level ON event_record (importance_level);
CREATE INDEX idx_event_action_type ON event_record (action_type);
CREATE INDEX idx_event_source_start ON event_record (source_id, event_start_time);
CREATE INDEX ix_event_record_event_type ON event_record (event_type);

CREATE TABLE video_session_file_rel (
	id SERIAL NOT NULL,
	session_id INTEGER NOT NULL,
	video_file_id INTEGER NOT NULL,
	sort_index INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES video_session (id),
	FOREIGN KEY(video_file_id) REFERENCES video_file (id)
);
CREATE UNIQUE INDEX uk_session_file_rel ON video_session_file_rel (session_id, video_file_id);
CREATE INDEX idx_session_file_rel_file ON video_session_file_rel (video_file_id);

CREATE TABLE event_tag_rel (
	id SERIAL NOT NULL,
	event_id INTEGER NOT NULL,
	tag_id INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_id) REFERENCES event_record (id),
	FOREIGN KEY(tag_id) REFERENCES tag_definition (id)
);
CREATE UNIQUE INDEX uk_event_tag_rel ON event_tag_rel (event_id, tag_id);
CREATE INDEX idx_event_tag_rel_tag ON event_tag_rel (tag_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_task_log_active_dedupe_key ON task_log (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');
"""

DROP_STATEMENTS = [
    "DROP INDEX IF EXISTS ux_task_log_active_dedupe_key",
    "DROP TABLE IF EXISTS event_tag_rel",
    "DROP TABLE IF EXISTS video_session_file_rel",
    "DROP TABLE IF EXISTS event_record",
    "DROP TABLE IF EXISTS video_source_runtime_state",
    "DROP TABLE IF EXISTS video_session",
    "DROP TABLE IF EXISTS video_file",
    "DROP TABLE IF EXISTS llm_usage_log",
    "DROP TABLE IF EXISTS daily_summary",
    "DROP TABLE IF EXISTS chat_query_log",
    "DROP TABLE IF EXISTS webhook_config",
    "DROP TABLE IF EXISTS video_source",
    "DROP TABLE IF EXISTS task_log",
    "DROP TABLE IF EXISTS tag_definition",
    "DROP TABLE IF EXISTS system_config",
    "DROP TABLE IF EXISTS mcp_call_log",
    "DROP TABLE IF EXISTS llm_provider",
    "DROP TABLE IF EXISTS home_profile",
    "DROP TABLE IF EXISTS home_entity_profile",
    "DROP TABLE IF EXISTS app_runtime_state",
    "DROP TABLE IF EXISTS admin_user",
]


def upgrade() -> None:
    for statement in BASELINE_SQL.split(";\n"):
        sql = statement.strip()
        if not sql:
            continue
        op.execute(sql)


def downgrade() -> None:
    for statement in DROP_STATEMENTS:
        op.execute(statement)

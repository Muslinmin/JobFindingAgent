from app.config import Settings


def test_timeout_ladder_settings_load_with_defaults():
    s = Settings()
    assert s.llm_call_timeout_s == 60
    assert s.agent_turn_deadline_s == 180
    assert s.backend_read_timeout_s == 240
    assert s.llm_retry_wait_s == 5
    assert s.llm_max_retries == 3


def test_timeout_ladder_ordering_holds():
    """concurrencyFor_agentV2.md §3: call < turn < client. If the client
    timeout is ever the smallest, the bot gives up on work the server is
    still doing (F-4)."""
    s = Settings()
    assert s.llm_call_timeout_s < s.agent_turn_deadline_s < s.backend_read_timeout_s

import json
import unittest
from dataclasses import replace

from lunabench import scenarios, targets


class GroundedExtraction(unittest.TestCase):
    def setUp(self):
        self.scenario = scenarios.SCENARIOS["extraction"]
        self.prepared = scenarios.prepare(self.scenario, 0, "warm", "request-a")
        self.answer = {
            "fixture_id": self.prepared.fixture_id,
            "facts": [
                {"field": "search_path", "value": "/search", "source_id": "EXA_SEARCH"},
                {"field": "domain_filter", "value": "includeDomains", "source_id": "EXA_SEARCH"},
                {"field": "default_num_results", "value": "10", "source_id": "EXA_SEARCH"},
                {"field": "contents_path", "value": "/contents", "source_id": "EXA_CONTENTS"},
                {"field": "contents_uncached_fallback", "value": "automatic live crawling", "source_id": "EXA_CONTENTS"},
                {"field": "guaranteed_freshness_seconds", "value": None, "source_id": None},
            ],
        }

    def validate(self):
        return scenarios.validate_output(self.scenario, self.prepared, json.dumps(self.answer))

    def test_supported_facts_accept_any_order_but_not_invented_guarantees(self):
        self.answer["facts"].reverse()
        self.assertEqual(self.validate(), (True, None))
        self.answer["facts"][0]["value"] = "0"
        self.answer["facts"][0]["source_id"] = "EXA_CONTENTS"
        self.assertEqual(self.validate(), (False, "incorrect_fact:guaranteed_freshness_seconds"))

    def test_schema_valid_but_wrong_value_or_source_fails(self):
        self.answer["facts"][0]["value"] = "/answer"
        self.assertFalse(self.validate()[0])
        self.answer["facts"][0]["value"] = "/search"
        self.answer["facts"][0]["source_id"] = "EXA_ANSWER"
        self.assertFalse(self.validate()[0])

    def test_missing_duplicate_extra_and_nonstring_facts_fail(self):
        original = json.dumps(self.answer)
        mutations = [
            lambda facts: facts.pop(),
            lambda facts: facts.append(dict(facts[0])),
            lambda facts: facts[0].update(extra="not permitted"),
            lambda facts: facts[2].update(value=10),
            lambda facts: facts[-1].update(value="null"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.answer = json.loads(original)
                mutate(self.answer["facts"])
                self.assertFalse(self.validate()[0])

    def test_wrong_case_and_ambiguous_json_fail(self):
        other = scenarios.prepare(self.scenario, 1, "warm", "request-a")
        self.assertFalse(scenarios.validate_output(self.scenario, other, json.dumps(self.answer))[0])
        encoded = json.dumps(self.answer)
        duplicate_key = '{"facts": [],' + encoded[1:]
        self.assertEqual(
            scenarios.validate_output(self.scenario, self.prepared, duplicate_key),
            (False, "invalid_json"),
        )
        self.assertFalse(scenarios.validate_output(self.scenario, self.prepared, "```json\n" + encoded + "\n```")[0])

    def test_stream_missing_default_and_real_units(self):
        prepared = scenarios.prepare(self.scenario, 2, "cold", "request-b")
        answer = {
            "fixture_id": prepared.fixture_id,
            "facts": [
                {"field": "media_type", "value": "text/event-stream", "source_id": "SSE"},
                {"field": "encoding", "value": "UTF-8", "source_id": "SSE"},
                {"field": "default_event_type", "value": "message", "source_id": "SSE"},
                {"field": "stop_reconnect_status", "value": "204", "source_id": "SSE"},
                {"field": "retry_numeric_base", "value": "10", "source_id": "SSE"},
                {"field": "fixed_initial_reconnect_ms", "value": None, "source_id": None},
            ],
        }
        self.assertEqual(scenarios.validate_output(self.scenario, prepared, json.dumps(answer)), (True, None))
        answer["facts"][-1].update(value="3000", source_id="SSE")
        self.assertFalse(scenarios.validate_output(self.scenario, prepared, json.dumps(answer))[0])


class PromptPolicies(unittest.TestCase):
    def test_cache_boundary_does_not_change_case_or_workload(self):
        scenario = scenarios.SCENARIOS["answer"]
        warm = scenarios.prepare(scenario, 0, "warm", "request-a")
        repeated = scenarios.prepare(scenario, 0, "warm", "request-a")
        other = scenarios.prepare(scenario, 1, "warm", "request-b")
        cold = scenarios.prepare(scenario, 0, "cold", "request-a")
        self.assertEqual(warm, repeated)
        self.assertEqual(warm.prompt.partition("\n\nQUERY\n")[0], other.prompt.partition("\n\nQUERY\n")[0])
        self.assertNotEqual(warm.fixture_id, other.fixture_id)
        self.assertNotEqual(warm.prompt, other.prompt)
        self.assertEqual(cold.prompt, "[request request-a]\n" + warm.prompt)
        self.assertEqual(cold.fixture_id, warm.fixture_id)
        self.assertEqual({cold.workload_id, warm.workload_id, other.workload_id}, {warm.workload_id})
        self.assertNotIn("request-a", warm.prompt.partition("\n\nQUERY\n")[0])

    def test_changed_task_does_not_reuse_workload_identity(self):
        scenario = scenarios.SCENARIOS["answer"]
        original = scenarios.prepare(scenario, 0, "warm", "request-a")
        changed = scenarios.prepare(replace(scenario, task=scenario.task + " Include a glossary."), 0, "warm", "request-a")
        self.assertNotEqual(original.workload_id, changed.workload_id)
        self.assertFalse(scenarios.validate_output(scenario, changed, "some output")[0])


class StructuralProseValidation(unittest.TestCase):
    def test_citations_and_length_not_factual_truth(self):
        scenario = scenarios.SCENARIOS["answer"]
        prepared = scenarios.prepare(scenario, 0, "warm", "request-a")
        # Deliberately unsupported prose demonstrates the declared structural-only scope.
        text = " ".join(["Unsupported claim."] * 75) + " [EXA_SEARCH] [EXA_CONTENTS] [EXA_ANSWER]"
        self.assertEqual(scenarios.validate_output(scenario, prepared, text), (True, None))
        self.assertEqual(
            scenarios.validate_output(scenario, prepared, text.replace("[EXA_SEARCH]", "[MADE_UP]")),
            (False, "unknown_source_citation"),
        )
        self.assertEqual(
            scenarios.validate_output(scenario, prepared, text.replace("[EXA_SEARCH]", "")),
            (False, "missing_required_source_citation"),
        )
        self.assertEqual(
            scenarios.validate_output(scenario, prepared, "Short. [EXA_SEARCH] [EXA_CONTENTS] [EXA_ANSWER]"),
            (False, "word_count_out_of_range"),
        )

    def test_synthesis_requires_case_specific_sources(self):
        scenario = scenarios.SCENARIOS["synthesis"]
        prepared = scenarios.prepare(scenario, 1, "warm", "request-a")
        text = " ".join(["Design recommendation."] * 225) + " [SSE] [EXA_ANSWER] [HTTP_IDEMPOTENT] [HTTP_RETRY] [HTTP_503]"
        self.assertEqual(scenarios.validate_output(scenario, prepared, text), (True, None))
        first_case = scenarios.prepare(scenario, 0, "warm", "request-a")
        self.assertFalse(scenarios.validate_output(scenario, first_case, text)[0])


class StructuredPayload(unittest.TestCase):
    def test_api_specific_strict_schema_contract(self):
        scenario = scenarios.SCENARIOS["extraction"]
        prepared = scenarios.prepare(scenario, 0, "warm", "request-a")
        chat = targets.build_body("chat", "model", prepared.prompt, 1024, "none", output_schema=prepared.output_schema)
        responses = targets.build_body("responses", "model", prepared.prompt, 1024, "none", output_schema=prepared.output_schema)
        chat_format = chat["response_format"]
        responses_format = responses["text"]["format"]
        self.assertEqual(chat_format["type"], "json_schema")
        self.assertEqual(responses_format["type"], "json_schema")
        self.assertTrue(chat_format["json_schema"]["strict"])
        self.assertTrue(responses_format["strict"])
        self.assertEqual(chat_format["json_schema"]["schema"], responses_format["schema"])
        schema = responses_format["schema"]
        fact_schema = schema["properties"]["facts"]["items"]
        for obj in (schema, fact_schema):
            self.assertFalse(obj["additionalProperties"])
            self.assertEqual(set(obj["required"]), set(obj["properties"]))
        self.assertEqual(fact_schema["properties"]["value"]["type"], ["string", "null"])
        self.assertIn(None, fact_schema["properties"]["source_id"]["enum"])
        self.assertNotIn("response_format", responses)
        self.assertNotIn("text", chat)

    def test_unstructured_tasks_do_not_request_json(self):
        scenario = scenarios.SCENARIOS["answer"]
        prepared = scenarios.prepare(scenario, 0, "warm", "request-a")
        chat = targets.build_body("chat", "model", prepared.prompt, 2048, "none", output_schema=prepared.output_schema)
        responses = targets.build_body("responses", "model", prepared.prompt, 2048, "none", output_schema=prepared.output_schema)
        self.assertNotIn("response_format", chat)
        self.assertNotIn("text", responses)


if __name__ == "__main__":
    unittest.main()

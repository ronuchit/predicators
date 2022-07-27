"""Open-loop large language model (LLM) meta-controller approach with prompt
modification where where predicate names are replaced with random strings.

Example command line:
    export OPENAI_API_KEY=<your API key>
    python src/main.py --approach llm_predicate_renaming --seed 0 \
        --strips_learner oracle \
        --env pddl_blocks_procedural_tasks \
        --num_train_tasks 3 \
        --num_test_tasks 1 \
        --debug
"""
import string
from typing import Dict

from predicators.src import utils
from predicators.src.approaches.llm_renaming_base_approach import \
    LLMBaseRenamingApproach


class LLMPredicateRenamingApproach(LLMBaseRenamingApproach):
    """LLMPredicateRenamingApproach definition."""

    @classmethod
    def get_name(cls) -> str:
        return "llm_predicate_renaming"

    def _create_replacements(self) -> Dict[str, str]:
        return {
            p.name: utils.generate_random_string(len(p.name),
                                                 list(string.ascii_lowercase),
                                                 self._rng)
            for p in self._get_current_predicates()
        }

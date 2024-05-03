from .generation import Generation
from .cot import COT  
from .tot import TOT
from .lookahead_eval import Lookahead_Eval
from .lookahead_eval_light import Lookahead_Eval_Light
from .lookahead_eval_ablation import Lookahead_Eval_Ablation
from .best_of_k import BestK
from common.registry import registry


def load_algorithm(name, config, llm_model):
    algorithm = registry.get_algorithm_class(name).from_config(llm_model, config)
    return algorithm
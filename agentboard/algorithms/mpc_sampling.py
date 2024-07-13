import pdb

from common.registry import registry
# from rouge import Rouge
import json
import torch
import random
import re
import io
import argparse
import numpy as np

@registry.register_algorithm("MPC_Sample")
class MPC_Sample:  # the algorithm should be stateless, and generates a whole plan / code / chain of actions at once.
    def __init__(self,
                 llm_model,
                 prompt_path=None,
                 lookahead_thought_length=3,
                 lookahead_token_length=None,    # the length of the lookahead token sequence, default use thought length as evaluation chunk
                 reward_threshold=1.0,
                 beam_temperature=0.7,
                 select_temperature=0.1,
                 n_generate_sample=8,
                 value_type = "logp",
                 do_sample=True,
                 ):
        
        self.llm_model = llm_model
        
        if prompt_path is not None:
            self.prompts = json.load(open(prompt_path, 'r'))
        else:
            self.prompts = {}
        
        self.task = "gsm8k"
        
        self.problem_size = 16 if self.task == "gsm8k" else 30
        
        self.n_gram = self.problem_size
        
        self.reward_threshold = reward_threshold 
        self.lookahead_decision_length = lookahead_thought_length
        self.lookahead_token_length = lookahead_token_length
        
        self.do_sample = do_sample
        self.select_temperature = select_temperature
        self.beam_temperature = beam_temperature
        self.select_temperature = select_temperature
        self.n_generate_sample = n_generate_sample
        self.value_type = value_type
        
        
        
    def make_prompt(self, prompt):
        with io.StringIO() as f:
            f.write(prompt)
            f.write("\n\n\n\n\n")
            # f.write(f'Q: {self.example}\n\n# solution in Python:\n\n\ndef solution():\n    """{self.example}"""\n')
            f.write(f'Solve this problem following previous examples:\nQ: {self.prompts["question"]}\n\n# Finish the solution in Python:\n\n\ndef solution():\n')
            for a in self.memory:
                if a is not None:
                    f.write(f"{a}")
            # get the prompt
            model_input = f.getvalue()
        return model_input

    def update_trajectory_pool(self, outputs, reward=None):
        
        # update the trajectory pool with the generated action rollouts by llm
        
        action_rollouts = outputs["action_chain"]
        
        history_rollouts = [a for a in self.memory if a is not None]
        
        item = []
        
        item.append({"Action": None, "Verified": False, "Reward": reward})
        
        for action in history_rollouts:
            item.append({"Action": action, "Verified": True, "Reward": reward})
            
        for action in action_rollouts:
            item.append({"Action": action, "Verified": None, "Reward": reward})
        
        self.trajectory_pool.append(item)
        
        
    def parse_action_sequence(self, action_output): 
        
        def _get_start_end_token_id(original_text, text, tokens):
            cnt_length = [len(token) for token in tokens]
            cumulated_cnt_length = np.cumsum(cnt_length)
            index_start =  original_text.index(text)#processed action index in action
            index_end = index_start + len(text)
                
            token_start = np.argmax(cumulated_cnt_length > index_start)
            token_end = np.argmax(cumulated_cnt_length > index_end)
            if token_end < token_start:
                token_end = -1
            return token_start, token_end
        
        if type(action_output) == str: # no logprob information
            
            prefix = "def solution():\n"
            
            all_prefix = [prefix] + [a for a in self.memory if a is not None]
            
            for prefix in all_prefix:
                if prefix in action: # added, in case there is repeat of prompt inside the generation
                    action = action.split(prefix)[1]
            action = action.lstrip('\n')
            
            # Here is the start of the action chain:
            if '\n' in action:
                all_actions = action.split('\n')
            else:
                all_actions = [action]
            
            first_action = all_actions[0] + '\n'
            action_chain = [a + '\n' for a in all_actions][: self.lookahead_decision_length] # only keep the first n actions
            
            return {"action": first_action, "action_chain": action_chain}, first_action
        
        elif type(action_output) == dict: # need logprob information
            
            action_text_output = action_output["text"]
            action_logprobs = action_output["logprobs"]
            action_tokens = action_output["tokens"]
            
            all_prefix = [prefix] + [a for a in self.memory if a is not None]
            
            token_start, token_end = 0, -1
            action = action_text_output
            
            for prefix in all_prefix:
                if prefix in action: # added, in case there is repeat of prompt inside the generation
                    action = action.split(prefix)[1]
                    
             # remove all '\n' in the beginning
            action = action.lstrip('\n')
            
            if self.lookahead_token_length is not None: # limit the length of the lookahead token sequence as a chunk for lookahead
                
                token_start, token_end = _get_start_end_token_id(action_text_output, action, action_tokens)
                
                token_end = min(token_end, token_start + self.lookahead_token_length)
                
                action = "".join(action_tokens[token_start:token_end])
                
            
                # Here is the start of the action chain:
                if '\n' in action:
                    all_actions = action.split('\n')
                else:
                    all_actions = [action]
                
                first_action = all_actions[0] + '\n'
                action_chain = [a + '\n' for a in all_actions]
                
                action_logprobs = action_logprobs[token_start:token_end]
                
                action_prob = np.exp(sum(action_logprobs))
            
                return {"action": first_action, "action_chain": action_chain, "action_prob": action_prob}, first_action
            
            else: # limit the number of thoughts in the lookahead
                
                # Here is the start of the action chain:
                if '\n' in action:
                    all_actions = action.split('\n')
                else:
                    all_actions = [action]
                
                first_action = all_actions[0] + '\n'
                action_chain = [a + '\n' for a in all_actions][: self.lookahead_decision_length] # only keep the first n actions
                
                token_start, token_end = _get_start_end_token_id(action_text_output, "".join(action_chain), action_tokens)
                
                action_logprobs = action_logprobs[token_start:token_end]
                
                action_prob = np.exp(sum(action_logprobs))
                
                return {"action": first_action, "action_chain": action_chain, "action_prob": action_prob}, first_action
        
        else:
            raise NotImplementedError
    
    def get_valid_actions(self, action_history):
        
        all_results = []
        
        for traj_id, trajectory in enumerate(self.trajectory_pool):
            
            # trajectory = trajectory[1:] # remove the first action, which is None
            
            start = max(1, len(trajectory) - self.n_gram + 1)
            
            for id in range(start):
                
                n = min([len(trajectory) - id, self.n_gram, len(action_history)+1])
                
                n_gram_list = [trajectory[id+s]["Action"] for s in range(n)]
                # n_gram_verification = [trajectory[id+s]["Verified"] for s in range(n)]
                n_gram_reward = [trajectory[id+s]["Reward"] for s in range(n)][-1]
                
                match = (action_history[-n+1:] == n_gram_list[:-1])
                # verified = False in n_gram_verification
                
                if match:
                    all_results.append((n_gram_list[-1], n_gram_reward))
                    
        return all_results
    
      
    def lookahead_decision_model(self, reward_threshold=1.0):
        # given the look ahead predictions, make the next action
        
        # ! todo: choose the best action when there are multiple options
        
        action_history = [None] + [action for action in self.memory if action is not None]
        
        all_valid_action_values = self.get_valid_actions(action_history)
        
        if len(all_valid_action_values) < 1:
            
            return None
        
        all_valid_values = np.array([item[1] for item in all_valid_action_values])
        all_valid_actions = [item[0] for item in all_valid_action_values]
        
        
        if all_valid_values.max() < reward_threshold:
            
            return None
        
        if self.do_sample: 
            probs = np.exp(all_valid_values/self.select_temperature)
            probs = probs / probs.sum()
            
            all_action_prob_pairs = dict()
            
            for (action, prob) in zip(all_valid_actions, probs):
                if action not in all_action_prob_pairs:
                    all_action_prob_pairs[action] = prob
                else:
                    all_action_prob_pairs[action] += prob
            
            # print in style action:prob, action: prob...
            print("Action probabilities: ", all_action_prob_pairs)
            
            all_valid_actions = list(all_action_prob_pairs.keys())
            probs = list(all_action_prob_pairs.values())
            
            sample = torch.multinomial(torch.tensor(probs), 1).item()
        
            action = all_valid_actions[sample]
        else:
            
            action = all_valid_actions[np.argmax(all_valid_values)]
            
        return action

    
    def reflection_tips(self, reward_threshold=0.5, window_size=2): 
        
        # determine if the model is stuck and requires self-reflection, used sparingly
                
        reflection = ""
        
        if len(self.trajectory_pool) > 0 :
            
            all_actions = ",".join(list(set([trajectory[-1]["Action"] for trajectory in self.trajectory_pool if trajectory[-1]["Action"] is not None])))
            
            reflection += f"I have generated {all_actions}, but none of them are correct. I need to revise them to solve the problem {self.prompts["question"]}."
            
            # if reflection is multiline, follow the format of python comment
            indent = "    "
            reflection = indent + "# " + reflection.replace("\n", "\n# ")

        if reflection != "":
            return True, reflection
        else:
            return False, None

    def run(self, question, prompts=None, **kwargs):
        
        if "end_suffix" in kwargs:
            end_suffix = kwargs["end_suffix"]
        else:
            end_suffix = None
            
        if prompts is not None:
            self.prompts = prompts
        self.prompts["question"] = question
        
        self.trajectory_pool = []
        
        args = {
            "n_generate_sample":self.n_generate_sample,
            "max_iters": self.problem_size,
            "max_tokens": 30*self.lookahead_decision_length if self.lookahead_token_length is None else self.lookahead_token_length,
            "temperature": self.beam_temperature,
            "top_p": 1.0,
            "stop": [],            
            "logprobs": (self.value_type == "logp"),
            "value_type": self.value_type
        }
        
        args = argparse.Namespace(**args)
        
        
        generation_config = {"n": args.n_generate_sample, 
                            "stop": args.stop, 
                            "top_p": args.top_p,
                            "max_tokens": args.max_tokens, 
                            "temperature": args.temperature,
                            "do_sample": True,
                            "logprobs": args.logprobs}
        
        all_iter = 0
        iter = 0
        
        
        self.memory = [None]*self.problem_size
        
        reflection_tips = ""

        while iter < args.max_iters:
            
            input_prompt = self.make_prompt(self.prompts["prompt"])
            system_message = self.prompts["system_msg"]
            success, action_sequence_samples = self.llm_model.generate_with_config(system_message, input_prompt, generation_config)
            
            if success:
                for action_sequence in action_sequence_samples:

                    processed_output, action = self.parse_action_sequence(action_sequence)

                    reward = 0
                    if args.value_type == "logp":
                        reward = processed_output["action_prob"]
                    else:
                        raise NotImplementedError
                    
                    self.update_trajectory_pool(processed_output, reward=reward)
            else:
                print("Failed to generate action sequence.")
                return False, None
                
            reward_threshold = self.reward_threshold if reflection_tips == "" else 0  # if reflection is needed, lower the threshold so that the model won't get stuck
            action = self.lookahead_decision_model(reward_threshold=self.reward_threshold)
            
            if action is not None:
                
                self.memory[iter] = action
                
                iter += 1
                
                reflection_tips = self.reflection_tips(reward_threshold=self.reward_threshold)
                
                if iter > args.max_iters:
                    break
                
                if end_suffix is not None and end_suffix in action:
                    break
            else:
                reflection_tips = self.reflection_tips(reward_threshold=self.reward_threshold)
                if reflection_tips[0]:
                    self.memory[iter] = reflection_tips[1]
                    break
                      
        if success:
            with io.StringIO() as f:
                f.write("def solution():\n")
                # iterate through the state
                for a  in self.memory:
                    if a is not None:
                        f.write(f"{a}\n")

            full_output = f.getvalue()

            return True, full_output
            
        return False, None
    
    @classmethod
    def from_config(cls, llm_model, config):
        return cls(llm_model, 
                   prompt_path=config.get("prompt_path", None),
                   lookahead_thought_length=config.get("lookahead_thought_length", 3),
                   lookahead_token_length=config.get("lookahead_token_length", None),
                   reward_threshold=config.get("reward_threshold", 1.0),
                   beam_temperature=config.get("beam_temperature", 0.7),
                   select_temperature=config.get("select_temperature", 0.1),
                   n_generate_sample=config.get("n_generate_sample", 8),
                   value_type=config.get("value_type", "logp"),
                   do_sample=config.get("do_sample", True),
                   )
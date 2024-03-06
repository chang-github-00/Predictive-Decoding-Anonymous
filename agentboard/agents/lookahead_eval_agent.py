import pdb

from agents.base_agent import BaseAgent
from common.registry import registry
# from rouge import Rouge
import json
import random
import re
import torch
from sentence_transformers import SentenceTransformer, util


@registry.register_agent("LookAheadEvalAgent")
class LookAheadEvalAgent(   # add world modeling objective in agent 
    BaseAgent):  # the agent should receive goal, state and action, then return the next state
    def __init__(self,
                 llm_model,
                 memory_size=100,
                 # set this to a very large number if you want to keep all history till context length limit
                 examples=[],
                 instruction="",
                 init_prompt_path=None,
                 system_message="You are a helpful assistant.",
                 need_goal=False,
                 check_actions=None,
                 check_inventory=None,
                 use_parser=True,
                 ):
        super().__init__()
        self.use_parser = use_parser
        self.llm_model = llm_model
        self.memory_size = memory_size
        self.goal = None
        self.init_obs = None
        if init_prompt_path is not None:  # load from file
            self.init_prompt_dict = json.load(open(init_prompt_path, 'r'))
            self.instruction = self.init_prompt_dict["instruction"]
            self.examples = self.init_prompt_dict["examples"]
        else:

            self.instruction = instruction
            self.examples = examples

            # self.reset(goal, init_obs)
            self.init_prompt_dict = {
                "examples": examples,
                "instruction": instruction,
                "system_msg": system_message
            }

        self.max_context_length = self.llm_model.context_length
        self.need_goal = need_goal
        self.check_actions = check_actions
        self.check_inventory = check_inventory

        self.example_prompt = None

        self.n_gram = 3
        self.trajectory_pool = []
        self.trajectory_reward = []
        
        self.use_guess_cnt = 0
        
        # self.guess_action = []

        self.similarity_threshold = 0.5 # determine if two observations are similar
        self.reward_threshold = 0.5 # determine if the reward is good enough
        self.window_size = 2 # determine the action window size for self-evaulation
        
        self.similarity_metric = SimilarityMetric()
        
        if "claude" in self.llm_model.engine:
            self.split = self.llm_model.xml_split
        else:
            self.split = {"example": [""],
                          "text": [""],
                          "rule": [""],
                          "system_msg": [""],
                          "instruction": [""],
                          "goal": [""]}

    def get_example_prompt(self): #return the prompt for an interaction turn
        return self.example_prompt
    
    def log_example_prompt(self, prompt):
        self.example_prompt = prompt

    def reset(self, goal, init_obs, init_act=None):
        self.goal = goal
        self.init_obs = init_obs
        self.memory = [("Action", init_act), ('Observation', self.init_obs)] if init_act \
            else [
            ('Observation', self.init_obs)]  # list of [('State', "xxx"), ('Action', "xxx"), ...]
        self.steps = 0
        self.done = False

        
        self.trajectory_pool = []
        self.trajectory_reward = []
        
        self.use_guess_cnt = 0


    def update(self, action, state):
        self.steps += 1

        self.memory.append(("Action", action))
        self.memory.append(("Observation", state))

    def make_prompt(self, need_goal=False, check_actions="check valid actions", check_inventory="inventory", system_message='', tip=None):
        query = ""
        query += self.split["instruction"][0] + self.instruction + self.split["instruction"][-1]

        if isinstance(self.examples, str):
            self.examples = [self.examples]

        if len(self.examples) > 0:
            query += "\nHere are examples:\n" + self.split["example"][0]
            for example in self.examples:
                query += example + "\n"
            query += self.split["example"][-1]
        if need_goal:
            query += self.split["goal"][0] + "You should perform actions to accomplish the goal: " + self.goal + "\n" + \
                     self.split["goal"][-1]
        if check_actions is not None:
            query += "You should use the following commands for help when your action cannot be understood: " + check_actions + "\n"
        if check_inventory is not None:
            query += "You should use the following commands for help when your action cannot be understood: inventory\n"

        history = self.memory[-self.memory_size:]
        input_prompt = query + "\n".join([item[0] + ": " + item[1] for item in history])
        
        if tip is not None:
            input_prompt += "\n Thought: " + tip

        input_prompt += "\nActions and Observations: "

        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": input_prompt}
        ]
        num_of_tokens = self.llm_model.num_tokens_from_messages(messages)
        while num_of_tokens > self.max_context_length - self.llm_model.max_tokens:
            history = history[1:]
            input_prompt = query + "\n".join([item[0] + ": " + item[1] for item in history])
            if tip is not None:
                input_prompt += "\n Thought: " + tip

            input_prompt += "\nActions and Observations: "
            
            # input_prompt += "\nPlease enter your action:"
            messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": input_prompt}
            ]
            num_of_tokens = self.llm_model.num_tokens_from_messages(messages)

        return input_prompt

    def action_parser_for_special_llms(self, action):
        
        '''
        This function is used to parse the action for special llms, e.g. codellama-13b, codellama-34b, llama, lemur, vicuna, etc.
        These llms often struggle to generate the format of the action correctly, so we need to parse the action to make it executable.
        '''
        
        origin_action = action
        if 'action' in action.lower():
            action_temp = action.split('\n')
            for act in action_temp:
                if "next action" in act and ':' in act: # zzh: in Claude will return "Here is the next action to take:"
                    idx = action_temp.index(act)
                    while idx + 1 < len(action_temp):
                        if action_temp[idx + 1]:
                            action = action_temp[idx + 1]
                            break
                        idx += 1
                if act.split(':')[0].lower().endswith('with action input'): # chang: in case parse tool output
                    action = act
                    break
                if 'action' in act.lower() and ':' in act:
                    action_temp = ':'.join(act.split(':')[1:])
                    if action_temp != "":
                        action = action_temp
                        break
                if 'action' in act.lower() and 'is to' in act:
                    action_temp = act.split('is to')[1]
                    if action_temp != "":
                        action = action_temp
                        break
                        
        if action.strip() == "":
            action = origin_action.split('\n')[0]   # temperary comment this line for codellama
        action = action.strip()
        action = action.strip("'/")
        action = action.split('\n')[0]
        return action

    
    # ---------------------------------------- components of lookahead agent  ----------------------------------------
    
    def parse_action_sequnece(self,action): 
        
        # parse the llm generated action sequence into a trajectory list
        
        action_sequences = action.split('\n')
        all_actions = []
        for action in action_sequences:
            try: 
                if "action:" in action.lower():
                    new_action = action.split(":")[1]
                    new_action = new_action.strip()
                    all_actions.append({"Action": new_action, "Verified": None, "Observation": None, "Reward": None})
                elif ":" in action.lower():
                    type = action.split(":")[0]
                    content = action.split(":")[1]
                    type = type.strip()
                    content = content.strip()
                    all_actions[-1][type.capitalize()] = content
            except:
                continue
                
        if len(all_actions)>0: 
            first_action = all_actions[0]["Action"] 
        else:
            first_action = ""
        return all_actions, first_action
    
    def update_trajectory_pool(self, action):
        
        # update the trajectory pool with the generated action rollouts by llm
        
        action_rollouts, new_action = self.parse_action_sequnece(action)
        
        begin_observation = self.memory[-1][1]
        
        action_rollouts.insert(0, {"Action":None, "Verified": True, "Observation": begin_observation, "Reward": None})
        
        self.trajectory_pool.append(action_rollouts)
        
        self.update_trajectory_reward()
        
        
    def update_trajectory_reward(self):
        
        # calculate the reward for new action rollouts, this function is called after each action execution

        new_trajectory = self.trajectory_pool[-1]
        
        observations = [item["Observation"] for item in new_trajectory if "Observation" in item]
        
        sim_to_goal= float(torch.max(self.similarity_metric.get_similarity(observations, self.goal)))
        
        self.trajectory_reward.append(sim_to_goal)
        
        # change the reward of the last trajectory
        
        for id in range(len(self.trajectory_pool[-1])):
            if "Reward" in self.trajectory_pool[-1][id]:
                self.trajectory_pool[-1][id]["Reward"] = sim_to_goal


    def verify_trajectory(self, threshold=0.5):
        
        # after the new execution, provide verification for action rollouts

        # if an action has been executed
        
        last_end_observation = self.memory[-1][1]
        
        if len(self.memory) > 2:
            last_executed_action = self.memory[-2][1]
            last_begin_observation = self.memory[-3][1]

            for traj_id, trajectory in enumerate(self.trajectory_pool):
                begin_observation = trajectory[0]["Observation"]
                
                for id, item in enumerate(trajectory):
                    if "Action" in item and item["Action"] == last_executed_action:
                        if "Observation" in item:
                            end_observation = item["Observation"]

                            # if begin_observation is the same as last_begin_observation, and end_observation is not the same as last_end_observation, then the action is not executed as expected
                            
                            begin_observation_similarity = float(torch.max(self.similarity_metric.get_similarity([begin_observation], [last_begin_observation])))
                            end_observation_similarity = float(torch.max(self.similarity_metric.get_similarity([end_observation], [last_end_observation])))
                            
                            if begin_observation_similarity > threshold and end_observation_similarity > threshold:
                                # this action is executed as expected, verify it as True
                                
                                if "Verified" in trajectory[id]:
                                    self.trajectory_pool[traj_id][id]["Verified"] = True
                                    
                            if begin_observation_similarity > threshold and end_observation_similarity < threshold:
                                # all the actions after the action should be verified as False
                                
                                for i in range(id, len(trajectory)):
                                    if "Verified" in trajectory[i]:
                                        self.trajectory_pool[traj_id][i]["Verified"] = False
                                        
                                break
                            
                        else:
                            continue
        else:
            return        
        
    
    def lookahead_decision_model(self, reward_threshold=0.5):
        # given the look ahead predictions, make the next action
        
        # ! todo: choose the best action when there are multiple options
        
        action_history = [item[1] for item in self.memory if item[0] == "Action"]
        
        for traj_id, trajectory in enumerate(self.trajectory_pool):
            
            trajectory = trajectory[1:] # remove the first action, which is None
            
            for id in range(len(trajectory) - self.n_gram):
                
                n_gram_list = [trajectory[id+s]["Action"] for s in range(self.n_gram)]
                n_gram_verification = [trajectory[id+s]["Verified"] for s in range(self.n_gram)]
                n_gram_reward = [trajectory[id+s]["Reward"] for s in range(self.n_gram)][-1]
                
                match =  (action_history[-self.n_gram+1:] == n_gram_list[:-1])
                verified = False in n_gram_verification
                reward_good = n_gram_reward > reward_threshold
                
                if match and not verified and reward_good:
                    return n_gram_list[-1]

        return None
        

    def reflection_tips(self, reward_threshold=0.5, window_size=2): 
        
        # determine if the model is stuck and requires self-reflection, used sparingly
        
        # first scenario: there are repeat cycles of actions that are not helping the model to reach the goal
        
        try:
            action_history = [item[1] for item in self.memory if item[0] == "Action"]
            
            
            if action_history.count(action_history[-1])>1 and action_history.count(action_history[-2])>1:
                action = "I have been repeating the same action, and it is not helping me to reach the goal. I need to perform diverse exploration."
            
                return True, action
        except:
            pass

        # second scenario: the last few-steps has been tested by execution, and the actual observations are not according to plan
        
        # third scenario: the last few-steps has been tested by the lookahead model, and the reward is not good
        
        if len(action_history) > window_size:
            
            last_actions = action_history[-window_size:] 
            
            for traj_id, trajectory in enumerate(self.trajectory_pool):

                trajectory = trajectory[1:] # remove the first action, which is None
                
                for id in range(len(trajectory) - window_size):
                    
                    n_gram_list = [trajectory[id+s]["Action"] for s in range(window_size)]
                    n_gram_verification = [trajectory[id+s]["Verified"] for s in range(window_size)]
                    n_gram_reward = [trajectory[id+s]["Reward"] for s in range(window_size)][-1]
                    
                    match = (last_actions == n_gram_list)
                    verified = False in n_gram_verification
                    reward_good = n_gram_reward > reward_threshold
                    
                    if match: 
                        if not verified:
                            # find the action that is not verified
                            error_action = n_gram_list[n_gram_verification.index(False)]
                            action = f"The execution of {error_action} is not as expected. I need to try something different."
                            return True, action
                        
                        if not reward_good:
                            action = "My original plan could not reach the goal. I should change my policy."
                            return True, action
                        
        return False, None
        
          
    def run(self, init_prompt_dict=None):
        
        self.verify_trajectory(threshold=self.similarity_threshold)

        # decide upon the best action based on simulated planning
        action = self.lookahead_decision_model(reward_threshold=self.reward_threshold)
        
        if action is not None:
            self.use_guess_cnt += 1
            return True, action, True
        
        else:
            need_tip, reflection_tip = self.reflection_tips(reward_threshold=self.reward_threshold, window_size=self.window_size)
            
            if init_prompt_dict is not None:
                self.init_prompt_dict = init_prompt_dict
                self.instruction = init_prompt_dict['instruction']
                self.examples = init_prompt_dict['examples']
                
            system_message = self.init_prompt_dict['system_msg']
            input_prompt = self.make_prompt(need_goal=self.need_goal,
                                            check_actions=self.check_actions,
                                            check_inventory=self.check_inventory,
                                            system_message=system_message,
                                            tip = reflection_tip)
            
            self.log_example_prompt(input_prompt)

            success, action_sequence = self.llm_model.generate(system_message, input_prompt)
            
            if success:

                action_ngram, action = self.parse_action_sequnece(action_sequence)
                
                self.update_trajectory_pool(action_sequence)
                
                if success and self.use_parser:
                    action = self.action_parser_for_special_llms(action)

            return success, action, False   

    @classmethod
    def from_config(cls, llm_model, config):
        memory_size = config.get("memory_size", 100)
        instruction = config.get("instruction", "")
        examples = config.get("examples", [])
        init_prompt_path = config.get("init_prompt_path", None)
        system_message = config.get("system_message", "You are a helpful assistant.")
        check_actions = config.get("check_actions", None)
        check_inventory = config.get("check_inventory", None)
        use_parser = config.get("use_parser", True)
        need_goal = config.get("need_goal", False)
        return cls(llm_model, memory_size, examples, instruction, init_prompt_path, system_message, 
                   need_goal, check_actions, check_inventory, use_parser)
        

class SimilarityMetric(object):
    def __init__(self,
                 model_name='sentence-transformers/all-MiniLM-L6-v2'):
        self.model = SentenceTransformer(model_name)

    def get_similarity(self, sequences, source_sequence):
        source_embedding = self.model.encode(source_sequence)
        sequence_embeddings = self.model.encode(sequences)
        
        similarity = util.pytorch_cos_sim(source_embedding, sequence_embeddings)
        return similarity
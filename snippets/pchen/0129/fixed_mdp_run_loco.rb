require_relative '../../rocky/utils'

itrs = 1000
batch_size = 50000
horizon = 100
discount = 0.99
seeds = (1..5).each do |i| i ** 2 * 5 + 23 end

mdps = []
# mdps << "mujoco_1_22.half_cheetah_mdp"
mdps << "mujoco_1_22.full_cheetah_mdp"
mdps << "mujoco_1_22.hopper_mdp"
# mdps << "mujoco_1_22.swimmer_mdp"
# mdps << "mujoco_1_22.humanoid_mdp"

algos = []
# # erwr
[0.2, 1].each do |best_quantile|
  algos << {
    _name: "erwr",
    max_opt_itr: 50,
    best_quantile: best_quantile,
    positive_adv: true,
  }
end
# # # trpo & ppo
[0.01, 0.1].each do |ss|
  algos << {
    _name: "ppo",
    step_size: ss,
  }
end
[0.1, 1].each do |ss|
  algos << {
    _name: "trpo",
    step_size: ss,
    backtrack_ratio: 0.8,
  }
end
# # # npg
[0.1, 0.01].each do |ss|
  [1e-1, 1e0, 1e-2].each do |lr|
    algos << {
      _name: "npg",
      step_size: ss,
      update_method: "adam",
      learning_rate: lr,
    }
  end
end
[0.1, 0.01].each do |ss|
  [1e-1, 1e0, 1e-2].each do |lr|
    algos << {
      _name: "npg",
      step_size: ss,
      update_method: "sgd",
      learning_rate: lr,
    }
  end
end
# # vpg
[1e-4, 1e-3, 1e-2, 1e-1].each do |lr|
  algos << {
    _name: "vpg",
    update_method: "adam",
    learning_rate: lr,
  }
end
[1e-4, 1e-3, 1e-2, 1e-1].each do |lr|
  algos << {
    _name: "vpg",
    update_method: "sgd",
    learning_rate: lr,
  }
end
# cem
# [0.05, 0.15].each do |best_frac|
#   [1].each do |extra_std|
#     [100, 500].each do |extra_decay_time|
#       algos << {
#         _name: "cem",
#         n_samples: 100,
#         best_frac: best_frac,
#         extra_std: extra_std,
#         extra_decay_time: extra_decay_time,
#       }
#     end
#   end
# end
# 
# 
inc = 0
seeds.each do |seed|
  mdps.each do |mdp|
    algos.each do |algo|
      exp_name = "mdp_fixed_0129_nn_pi_loco_#{inc = inc + 1}"
      params = {
        mdp: {
          _name: mdp,
        },
        normalize_mdp: true,
        policy: {
          _name: "mean_std_nn_policy",
          hidden_sizes: [32, 32],
        },
        baseline: {
          _name: "linear_feature_baseline",
        },
        exp_name: exp_name,
        algo: {
          whole_paths: true,
          max_path_length: horizon,
          n_itr: itrs,
          discount: discount,
          plot: true,
        }.merge(algo),
        n_parallel: 8,
        snapshot_mode: "last",
        seed: seed,
        plot: true,
      }
      command = to_command(params)
      # puts command
      # system(command)
      dockerified = """docker run \
  -v ~/.bash_history:/root/.bash_history \
  -v /slave/theano_cache_docker:/root/.theano \
  -v /slave/theanorc:/root/.theanorc \
  -v ~/.vim:/root/.vim \
  -v /slave/gitconfig:/root/.gitconfig \
  -v ~/.vimrc:/root/.vimrc \
  -v /slave/dockerfiles/ssh:/root/.ssh \
  -v /slave/jupyter:/root/.jupyter \
  -v /home/ubuntu/data:/root/workspace/data \
  -v /slave/workspace:/root/workspace \
  -v `pwd`/rllab:/root/workspace/rllab \
  --device /dev/nvidia0:/dev/nvidia0 \
  --device /dev/nvidiactl:/dev/nvidiactl \
  --device /dev/nvidia-uvm:/dev/nvidia-uvm \
  dementrock/starcluster:new #{command}"""
      fname = "#{exp_name}.sh"
      f = File.open(fname, "w")
      f.puts dockerified
      f.close
      system("chmod +x " + fname)
      system("qsub -V -b n -l mem_free=8G,h_vmem=14G -r y -cwd " + fname)
    end
  end
end

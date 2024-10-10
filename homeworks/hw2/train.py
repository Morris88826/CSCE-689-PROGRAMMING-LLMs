import os
import time
import math
import torch
import yaml
import argparse
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
from transformers import GPT2Tokenizer
from libs.utils import print0
from libs.model import GPT, GPTConfig
from libs.dataloader import DistributedDataLoader


def get_lr(step, max_lr=6e-4, p=0.1, warmup_steps=10, max_steps=50): # cosine decay schedule
    min_lr = max_lr * p
    if step < warmup_steps:
        lr = min_lr + (max_lr - min_lr) * step / warmup_steps
    elif step > max_steps:
        lr = min_lr
    else:
        decay_ratio = (step - warmup_steps) / (max_steps - warmup_steps)
        assert 0 <= decay_ratio <= 1
        coeff = 0.5 * (1 + math.cos(math.pi * decay_ratio))
        lr = min_lr + (max_lr - min_lr) * coeff
    return lr

# torchrun command sets the following environment variables: RANK, LOCAL_RANK, WORLD_SIZE
# torchrun --standalone --nproc_per_node=4 train.py
def init_ddp():
    # RANK is the global rank of the current process across all nodes
    # LOCAL_RANK is the rank of the current process on the current node
    # WORLD_SIZE is the number of processes participating in the run (usually the number of GPUs)
    ddp = int(os.environ.get("RANK", -1)) != -1 # is this a ddp run?
    if ddp:
        assert torch.cuda.is_available(), "DistributedDataLoader requires CUDA"
        init_process_group(backend="nccl")
        ddp_rank = int(os.environ["RANK"])
        ddp_local_rank = int(os.environ["LOCAL_RANK"])
        ddp_world_size = int(os.environ["WORLD_SIZE"])
        device = f'cuda:{ddp_local_rank}'
        torch.cuda.set_device(device)
        master_process = ddp_rank == 0
    else:
        ddp_rank = 0
        ddp_local_rank = 0
        ddp_world_size = 1
        master_process = True
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return ddp, ddp_rank, ddp_local_rank, ddp_world_size, master_process, device

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='./configs/config.yaml', help='config file')
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    if config["tensorcores"]:
        torch.set_float32_matmul_precision('high')

    # ddp
    ddp, ddp_rank, ddp_local_rank, ddp_world_size, master_process, device = init_ddp()
    # print("This is GPU", ddp_local_rank, "out of", ddp_world_size)
    # destroy_process_group() # clean up
    # import sys; sys.exit(0)

    
    assert config["total_batch_size"] % (config["batch_size"]*config["sequence_length"]*ddp_world_size) == 0, "total_batch_size must be divisible by B*T*world_size"
    grad_accumulation_steps = config["total_batch_size"] // (config["batch_size"]*config["sequence_length"]*ddp_world_size)
    print0(f"Total batch size: {config['total_batch_size']}, grad_accumulation_steps: {grad_accumulation_steps}")



    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device_type = "cuda" if torch.cuda.is_available() else "cpu"

    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")

    train_loader = DistributedDataLoader(config["input_bin"], config["batch_size"], config["sequence_length"], ddp_rank, ddp_world_size)
    val_loader = DistributedDataLoader(config["input_val_bin"], config["batch_size"], config["sequence_length"], ddp_rank, ddp_world_size)

    # create the model
    model = GPT(GPTConfig(), norm_method=config["norm_method"], use_FLASH=config["flash"], use_RoPE=config["use_RoPE"])
    model.to(device)
    model = torch.compile(model)
    if ddp:
        model = DDP(model, device_ids=[ddp_local_rank])
    raw_model = model.module if ddp else model
    optimizer = raw_model.configure_optimizers(weight_decay=config["weight_decay"], learning_rate=config["learning_rate"], betas=(0.9, 0.95), eps=1e-8)


    # create the logging directory if it does not exist
    logfile = None
    if config["output_dir"] is not None:
        os.makedirs(config["output_dir"], exist_ok=True)
        logfile = os.path.join(config["output_dir"], "main.log")
        # create the log file "main.log" inside it, and wipe it clean
        with open(logfile, "w") as f:
            pass
    

    for step in range(config["num_iterations"]+1):
        last_step = step == config["num_iterations"]
        # once in a while, check the validation loss
        if (step % config["val_loss_every"] == 0 or last_step) and (val_loader is not None):
            model.eval()
            val_loader.reset()
            with torch.no_grad():
                val_loss_accum = 0.0
                for _ in range(config["val_max_steps"]):
                    x, y = val_loader.next_batch()
                    x, y = x.to(device), y.to(device)
                    with torch.autocast(device_type=device_type, dtype=torch.bfloat16):
                        logits, loss = model(x, y)
                    
                    loss /= config["val_max_steps"]
                    val_loss_accum += loss.detach()
                if ddp:
                    dist.all_reduce(val_loss_accum, op=dist.ReduceOp.AVG)
            print0(f"Validation loss: {val_loss_accum.item():.6f}")
            if master_process and logfile is not None:
                # save the model checkpoint
                ckpt_path = os.path.join(config["output_dir"], f"step_{step}.pth")
                print0(f"Saving checkpoint to {ckpt_path}")
                torch.save(model.state_dict(), ckpt_path)

                with open(logfile, "a") as f:
                    f.write("step: %d | val loss: %.6f\n" % (step, val_loss_accum.item()))
        if last_step:
            break

        model.train()
        optimizer.zero_grad()
        loss_accum = 0.0
        t0 = time.time()
        for micro_step in range(grad_accumulation_steps):
            x, y = train_loader.next_batch()
            x, y = x.to(device), y.to(device)
            with torch.autocast(device_type=device_type, dtype=torch.bfloat16):
                logits, loss = model(x, y)
            loss = loss / grad_accumulation_steps # the normalizing factor
            loss_accum += loss.detach()

            if ddp:
                model.require_backward_grad_sync = (micro_step == grad_accumulation_steps-1) # don't sync until the last micro-step
            loss.backward()

        if ddp:
            dist.all_reduce(loss_accum, op=dist.ReduceOp.AVG) # average the loss across all processes

        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config["grad_clip"]) # clip the gradients, prevent exploding

        lr = get_lr(step, max_lr=config["learning_rate"], p=config["learning_rate_decay_frac"], warmup_steps=config["warmup_iters"], max_steps=config["num_iterations"])
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        optimizer.step()
        
        torch.cuda.synchronize()
        t1 = time.time()
        dt = t1 - t0
        token_processed = grad_accumulation_steps * train_loader.B * train_loader.T * ddp_world_size
        token_throughput = token_processed / dt
        print0(f"Epoch {step}, Loss: {loss_accum.item():.6f}, lr: {lr:4f}, norm: {norm:.4f}, dt: {dt*1000:.2f}ms, token throughput: {token_throughput:.2f} tokens/s")
        if master_process and logfile is not None:
            with open(logfile, "a") as f:
                f.write("step: %d | train loss: %.6f\n" % (step, loss_accum.item()))
    if ddp:
        destroy_process_group()
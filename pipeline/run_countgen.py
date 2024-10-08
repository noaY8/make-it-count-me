import sys

sys.path.append('.')
from utils.counting_words_extract import find_nummod, word2number
from pipeline.mask_extraction.extract_mask import relayout
import torch
import argparse
import json
import numpy as np
import random
import yaml
import diffusers
from diffusers.utils.torch_utils import randn_tensor
import os
from pipeline.self_counting_sdxl_pipeline import SelfCountingSDXLPipeline
from utils.generate_random_masks import generate_random_masks_factory, show_mask, show_mask_list
from tqdm import tqdm
from torch.cuda.amp import autocast #added by noa 08.08.24
from accelerate import cpu_offload #added by noa 08.08.24
#from google.cloud import storage
import gc  #added by noa 27.08.24

# Set max split size to reduce fragmentation
#torch.cuda.set_per_process_memory_fraction(0.95)
#torch.cuda.set_max_split_size_mb(64)  # Adjust the value as needed
#os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:64' #added by noa 08.08.24
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:32"

def upload_to_gcs(local_file_path, bucket_name, destination_blob_name): #added by noa 08.08.24 - start
    """Uploads a file to the bucket."""
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(local_file_path)

    print(f"File {local_file_path} uploaded to {destination_blob_name}.") #added by noa 08.08.24 - end

def read_yaml(file_path):
    with open(file_path, "r") as yaml_file:
        yaml_data = yaml.safe_load(yaml_file)
    return yaml_data

#def move_model_to_cpu(model): #added by noa 08.08.24
#    for module in model.modules():
#        if hasattr(module, 'to'):
#            module.to('cpu')
            
def set_seed(seed: int):
    """
    Set the seed for reproducibility in PyTorch, NumPy, and Python's random module.

    Parameters:
    - seed (int): The seed value to use for all random number generators.
    """
    # PyTorch
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)  # For multi-GPU setups
        # Make CuDNN backend deterministic
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    np.random.seed(seed)
    random.seed(seed)


def init_sdxl_model(config):
    sdxl_pipe = SelfCountingSDXLPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0", use_safetensors=True,
        torch_dtype=torch.float16, variant="fp16", use_onnx=False
    )

    device = torch.device(config["pipeline"]["device"])
    sdxl_pipe.to(device)
    
    # Set the model to evaluation mode
    #sdxl_pipe.eval()  # added by noa 08.08.24

    #sdxl_pipe.gradient_checkpointing_enable()  # Enable gradient checkpointing, added by noa 08.08.24
    #cpu_offload(sdxl_pipe)  # Offload model to CPU, added by noa 08.08.24

    #move_model_to_cpu(sdxl_pipe) #added by noa 08.08.24

    sdxl_pipe.counting_config = config['counting_model']

    if config['counting_model']['use_ddpm']:
        print('Using DDPM as scheduler.')
        sdxl_pipe.scheduler = diffusers.DDPMScheduler.from_config(sdxl_pipe.scheduler.config)

    return sdxl_pipe, device


def run_counting_pipeline_corrected_masks(sdxl_pipe, prompt, generator, object_masks, latents, config):
    with torch.no_grad(): #added by noa 08.08.24
        with autocast(): #added by noa 08.08.24
            out = sdxl_pipe(prompt=[prompt],
                            num_inference_steps=config["counting_model"]["num_inference_steps"],
                            perform_counting=True,
                            desired_mask=object_masks,
                            generator=generator,
                            latents=latents).images
            
            #print("Memory summary after inference:")  # added by noa 08.08.24
            #print(torch.cuda.memory_summary(device='cuda', abbreviated=False))  # added by noa 08.08.24


    image = out[0]
    return image


def run_pipeline(prompt_objects, config, phase1_type, phase2_type):
    # make directory
    out_dir = config["pipeline"]["output_path"]
    os.makedirs(f'{out_dir}', exist_ok=True)

    metadata_json = []

    if os.path.isfile(f"{out_dir}/metadata.json"):
        with open(f"{out_dir}/metadata.json", "r") as f:
            metadata_json = json.load(f)

    # Initialize SDXL model
    sdxl_pipe, device = init_sdxl_model(config)

    for prompt_object in tqdm(prompt_objects, desc="Iterating prompt dataset"):
        #---------------------------------------------------------------------------noa changed start
        # Change 1: Check available GPU memory before processing
        torch.cuda.empty_cache()  # Clear cache to get an accurate memory reading
        torch.cuda.synchronize()  # Ensure all kernels have finished
        available_memory = torch.cuda.get_device_properties(0).total_memory - torch.cuda.memory_allocated()

        if available_memory < 80 * 1024 * 1024:  # 80 MB threshold
            print(f"Skipping sample due to low memory. Available: {available_memory / (1024 * 1024)} MB")
            continue
        #---------------------------------------------------------------------------noa changed end

        prompt = prompt_object['prompt']
        seed = prompt_object['seed']

        # Create latents
        set_seed(seed)
        generator = torch.Generator().manual_seed(seed)
        shape = (1, sdxl_pipe.unet.config.in_channels, 128, 128)
        latents = randn_tensor(shape, generator=generator, device=device, dtype=torch.float16)

        # Extract object + number
        required_object_num = prompt_object['int_number']
        obj_name = prompt_object['object']

        if required_object_num > 9:
            print(f"Skipping {obj_name} with {required_object_num} objects as it is not supported.")
            continue

        img_id = f'{obj_name}_num={required_object_num}_seed={seed}'
        vanilla_img = None

        if phase1_type == 'random_mask':
            object_masks = generate_random_masks_factory(shape=config['mask_creation']['random_mask']['shape'],
                                                         number_clusters=required_object_num)
            show_mask(object_masks, f"{config['pipeline']['output_path']}/{img_id}_mask.png")
            obj_num_match = False
        elif phase1_type == 'dbscan_mask':
            vanilla_masks, correct_number_mask, object_masks, vanilla_img, obj_num_match = relayout(sdxl_pipe, prompt,
                                                                                                    required_object_num,
                                                                                                    config, seed)
            # show_mask_list([vanilla_masks, correct_number_mask, object_masks],
            #                titles=[f'Vanilla mask: {int(vanilla_masks.max())}',
            #                        f'Correct number mask: {int(correct_number_mask.max())}',
            #                        f'Postprocess mask: {int(object_masks.max())}'],
            #                save_path=f"{config['pipeline']['output_path']}/{img_id}_masks.png") #noa removed
        elif phase1_type == 'no_mask':
            object_masks = None

        if obj_num_match:
            image = vanilla_img
        else:
            if phase2_type == 'ours_counting_loss':
                image = run_counting_pipeline_corrected_masks(sdxl_pipe, prompt, generator, object_masks, latents,
                                                              config)
            elif phase2_type == 'vanilla':
                pass
                
        #local_file_path = f"{out_dir}/{img_id}.png" #noa added 08.08.24
        image.save(f"{out_dir}/{img_id}.png")

        # Upload to GCS
        #bucket_name = 'make-it-count-orig'  #noa added 08.08.24 - start
        #destination_blob_name = f"{img_id}.png"
        #upload_to_gcs(local_file_path, bucket_name, destination_blob_name) #noa added 08.08.24 - end

        #--------------------------   Noa removed START    

        # vanilla_img.save(f"{out_dir}/{img_id}_vanilla.png")
        # metadata_item = {
        #     'id': img_id,
        #     'prompt': prompt,
        #     'seed': seed,
        #     'obj_class': obj_name,
        #     'requiered_object_num': required_object_num
        # }

        # metadata_json.append(metadata_item)

        # with open(f"{out_dir}/metadata.json", "w") as f:
        #     json.dump(metadata_json, f, indent=4)

        #--------------------------   Noa removed END 

        #--------------------------   Noa added START 
        # Change 2: Clear cache and delete variables to save memory
        del prompt, seed, generator, shape, latents, required_object_num, obj_name, img_id, vanilla_img
        torch.cuda.empty_cache()
        gc.collect()  # Garbage collection to free up CPU memory

    torch.cuda.empty_cache()
    gc.collect()  # Final garbage collection after loop ends
    #--------------------------   Noa added END 
        
def parse_arguments():
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument("--prompt", type=str, default="A photo of six kittens sitting on a branch")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--config", type=str, default="pipeline/pipeline_config.yaml")
    parser.add_argument("--dataset_file", type=str, default="")
    parser.add_argument("--output_path", type=str, default=None)

    return parser.parse_args()


if __name__ == "__main__":
    more_then_one = True

    args = parse_arguments()

    config = read_yaml(args.config)

    if args.output_path is not None:
        config["pipeline"]["output_path"] = args.output_path

    if (args.dataset_file is not None) and (len(args.dataset_file) > 0):
        with open(args.dataset_file, "r") as f:
            prompt_objects = json.load(f)[:]

    else:
        int_number, object_singular = find_nummod([args.prompt])[0]
        prompt_objects = [{
            "prompt": args.prompt,
            "object": object_singular,
            "int_number": word2number.get(int_number, 0),
            "seed": args.seed
        }, ]
        print(prompt_objects)

    phase1_type = config['pipeline']['phase1_type']
    phase2_type = config['pipeline']['phase2_type']

    # if more_then_one:
    #     for promt_object in prompt_objects:
    #         print(promt_object)
    #         run_pipeline(promt_object, config, phase1_type, phase2_type)
    # else:
    #     run_pipeline(prompt_objects, config, phase1_type, phase2_type)
    run_pipeline(prompt_objects, config, phase1_type, phase2_type)
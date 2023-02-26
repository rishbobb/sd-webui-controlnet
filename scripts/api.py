import numpy as np
from typing import Any, List, Dict, Set, Union
from fastapi import FastAPI, Body, HTTPException, Request, Response
import base64
import io
from io import BytesIO
from PIL import PngImagePlugin, Image
import piexif
import piexif.helper

import gradio as gr

from modules.api.models import *
from modules.processing import StableDiffusionProcessingTxt2Img, StableDiffusionProcessingImg2Img, process_images

from modules import sd_samplers
from modules.shared import opts, cmd_opts
import modules.shared as shared
import modules.scripts as scripts

from scripts.controlnet import update_cn_models, cn_models_names
from scripts.processor import *

from threading import Lock
from modules.call_queue import queue_lock
from pydantic import BaseModel
import PIL.ImageOps

class ProgressResponse(BaseModel):
    progress: float = Field(title="Progress", description="The progress with a range of 0 to 1")
    eta_relative: float = Field(title="ETA in secs")
    state: dict = Field(title="State", description="The current state snapshot")
    current_image: str = Field(default=None, title="Current image", description="The current image in base64 format. opts.show_progress_every_n_steps is required for this to work.")
    textinfo: str = Field(default=None, title="Info text", description="Info text used by WebUI.")

def validate_sampler_name(name):
    config = sd_samplers.all_samplers_map.get(name, None)
    if config is None:
        raise HTTPException(status_code=404, detail="Sampler not found")

    return name

def decode_base64_to_image(encoding):
    if encoding.startswith("data:image/"):
        encoding = encoding.split(";")[1].split(",")[1]
    try:
        image = Image.open(BytesIO(base64.b64decode(encoding)))
        return image
    except Exception as err:
        raise HTTPException(status_code=500, detail="Invalid encoded image")
    
def encode_to_base64(image):
    if type(image) is str:
        return image
    elif type(image) is Image.Image:
        return encode_pil_to_base64(image)
    elif type(image) is np.ndarray:
        return encode_np_to_base64(image)
    else:
        return ""
    
def encode_pil_to_base64(image):
    with io.BytesIO() as output_bytes:

        if opts.samples_format.lower() == 'png':
            use_metadata = False
            metadata = PngImagePlugin.PngInfo()
            for key, value in image.info.items():
                if isinstance(key, str) and isinstance(value, str):
                    metadata.add_text(key, value)
                    use_metadata = True
            image.save(output_bytes, format="PNG", pnginfo=(metadata if use_metadata else None), quality=opts.jpeg_quality)

        elif opts.samples_format.lower() in ("jpg", "jpeg", "webp"):
            parameters = image.info.get('parameters', None)
            exif_bytes = piexif.dump({
                "Exif": { piexif.ExifIFD.UserComment: piexif.helper.UserComment.dump(parameters or "", encoding="unicode") }
            })
            if opts.samples_format.lower() in ("jpg", "jpeg"):
                image.save(output_bytes, format="JPEG", exif = exif_bytes, quality=opts.jpeg_quality)
            else:
                image.save(output_bytes, format="WEBP", exif = exif_bytes, quality=opts.jpeg_quality)

        else:
            raise HTTPException(status_code=500, detail="Invalid image format")

        bytes_data = output_bytes.getvalue()

    return base64.b64encode(bytes_data)

def encode_np_to_base64(image):
    pil = Image.fromarray(image)
    return encode_pil_to_base64(pil)

def controlnet_api(_: gr.Blocks, app: FastAPI):

    # @app.get("/controlnet/progress")
    # async def progress():
    #     if shared.state.job_count == 0:
    #         return ProgressResponse(progress=0, eta_relative=0, state=shared.state.dict(), textinfo=shared.state.textinfo)

    #     # avoid dividing zero
    #     progress = 0.01

    #     if shared.state.job_count > 0:
    #         progress += shared.state.job_no / shared.state.job_count
    #     if shared.state.sampling_steps > 0:
    #         progress += 1 / shared.state.job_count * shared.state.sampling_step / shared.state.sampling_steps

    #     time_since_start = time.time() - shared.state.time_start
    #     eta = (time_since_start/progress)
    #     eta_relative = eta-time_since_start

    #     progress = min(progress, 1)

    #     shared.state.set_current_image()

    #     current_image = None
    #     if shared.state.current_image and not req.skip_current_image:
    #         current_image = encode_pil_to_base64(shared.state.current_image)

    #     return ProgressResponse(progress=progress, eta_relative=eta_relative, state=shared.state.dict(), current_image=current_image, textinfo=shared.state.textinfo)

    @app.post("/controlnet/txt2img")
    def txt2img(
        prompt: str = Body("", title='Prompt'),
        negative_prompt: str = Body("", title='Negative Prompt'),
        controlnet_input_image: List[str] = Body([], title='ControlNet Input Image'),
        controlnet_mask: List[str] = Body([], title='ControlNet Input Mask'),
        controlnet_module: str = Body("", title='Controlnet Module'),
        controlnet_model: str = Body("", title='Controlnet Model'),
        controlnet_weight: float = Body(1.0, title='Controlnet Weight'),
        controlnet_resize_mode: str = Body("Scale to Fit (Inner Fit)", title='Controlnet Resize Mode'),
        controlnet_lowvram: bool = Body(False, title='Controlnet Low VRAM'),
        controlnet_processor_res: int = Body(64, title='Controlnet Processor Res'),
        controlnet_threshold_a: float = Body(64, title='Controlnet Threshold a'),
        controlnet_threshold_b: float = Body(64, title='Controlnet Threshold b'),
        controlnet_guidance: float = Body(1.0, title='ControlNet Guidance Strength'),
        controlnet_guessmode: bool = Body(True, title="Guess Mode"),
        #hiresfix
        enable_hr: bool = Body(False, title="hiresfix"),
        denoising_strength: float = Body(0.5, title="Denoising Strength"),
        hr_scale: float = Body(1.5, title="HR Scale"),
        hr_upscale: str = Body("Latent", title="HR Upscale"),
        seed: int = Body(-1, title="Seed"),
        subseed: int = Body(-1, title="Subseed"),
        subseed_strength: int = Body(-1, title="Subseed Strength"),
        sampler_index: str = Body("", title='Sampler Name'),
        batch_size: int = Body(1, title="Batch Size"),
        n_iter: int = Body(1, title="Iteration"),
        steps: int = Body(20, title="Steps"),
        cfg_scale: float = Body(7, title="CFG"),
        width: int = Body(512, title="width"),
        height: int = Body(512, title="height"),
        restore_faces: bool = Body(True, title="Restore Faces"),
        override_settings: Dict[str, Any] = Body(None, title="Override Settings"),
        override_settings_restore_afterwards: bool = Body(True, title="Restore Override Settings Afterwards"),    
        ):

        with queue_lock:
            p = StableDiffusionProcessingTxt2Img(
                sd_model=shared.sd_model,
                outpath_samples=opts.outdir_samples or opts.outdir_txt2img_samples,
                outpath_grids=opts.outdir_grids or opts.outdir_txt2img_grids,
                prompt=prompt,
                styles=[],
                negative_prompt=negative_prompt,
                seed=seed,
                subseed=subseed,
                subseed_strength=subseed_strength,
                seed_resize_from_h=-1,
                seed_resize_from_w=-1,
                seed_enable_extras=False,
                sampler_name=sampler_index,
                batch_size=batch_size,
                n_iter=n_iter,
                steps=steps,
                cfg_scale=cfg_scale,
                width=width,
                height=height,
                restore_faces=restore_faces,
                tiling=False,
                enable_hr=enable_hr,
                denoising_strength=denoising_strength,
                hr_scale=hr_scale,
                hr_upscaler=hr_upscale,
                hr_second_pass_steps=0,
                hr_resize_x=0,
                hr_resize_y=0,
                override_settings=override_settings,
                do_not_save_samples=True,
                do_not_save_grid=True,
            )

            shared.state.begin()
            cn_image = Image.open(io.BytesIO(base64.b64decode(controlnet_input_image[0])))        
            cn_image_np = np.array(cn_image).astype('uint8')

            if controlnet_mask == []:
                cn_mask_np = np.zeros(shape=(512, 512, 3)).astype('uint8')
            else:
                cn_mask = Image.open(io.BytesIO(base64.b64decode(controlnet_mask[0])))        
                cn_mask_np = np.array(cn_mask).astype('uint8')
        
            cn_args = {
                "control_net_enabled": True,
                "control_net_module": controlnet_module,
                "control_net_model": controlnet_model,
                "control_net_weight": controlnet_weight,
                "control_net_image": {'image': cn_image_np, 'mask': cn_mask_np},
                "control_net_scribble_mode": False,
                "control_net_resize_mode": controlnet_resize_mode,
                "control_net_rgbbgr_mode": False,
                "control_net_lowvram": controlnet_lowvram,
                "control_net_pres": controlnet_processor_res,
                "control_net_pthr_a": controlnet_threshold_a,
                "control_net_pthr_b": controlnet_threshold_b,
                "control_net_guidance_strength": controlnet_guidance,
                "control_net_guess_mode": controlnet_guessmode,
                "control_net_api_access": True,
            }

            p.scripts = scripts.scripts_txt2img
            p.script_args = [0, ]
            for k, v in cn_args.items():
                setattr(p, k, v)

            if cmd_opts.enable_console_prompts:
                print(f"\ntxt2img: {prompt}", file=shared.progress_print_out)

            processed = process_images(p)            
            p.close()

            generation_info_js = processed.js()
            if opts.samples_log_stdout:
                print(generation_info_js)

            if opts.do_not_show_images:
                processed.images = []
            shared.state.end()

        b64images = list(map(encode_to_base64, processed.images))
        
        return {"images": b64images, "info": processed.js()}


    @app.post("/controlnet/img2img")
    def img2img(
        init_images: List[str] = Body([], title='Init Images'),
        mask: str = Body(None, title='Mask'),
        mask_blur: int = Body(30, title='Mask Blur'),
        inpainting_fill: int = Body(0, title='Inpainting Fill'),
        inpaint_full_res: bool = Body(True, title='Inpainting Full Resolution'),
        inpaint_full_res_padding: int = Body(1, title='Inpainting Full Resolution Padding'),
        inpainting_mask_invert: int = Body(1, title='Mask Invert'),
        resize_mode: int = Body(0, title='Resize Mode'),
        denoising_strength: float = Body(0.7, title='Denoising Strength'),
        prompt: str = Body("", title='Prompt'),
        negative_prompt: str = Body("", title='Negative Prompt'),
        controlnet_input_image: List[str] = Body([], title='ControlNet Input Image'),
        controlnet_mask: List[str] = Body([], title='ControlNet Input Mask'),
        controlnet_module: str = Body("", title='Controlnet Module'),
        controlnet_model: str = Body("", title='Controlnet Model'),
        controlnet_weight: float = Body(1.0, title='Controlnet Weight'),
        controlnet_resize_mode: str = Body("Scale to Fit (Inner Fit)", title='Controlnet Resize Mode'),
        controlnet_lowvram: bool = Body(False, title='Controlnet Low VRAM'),
        controlnet_processor_res: int = Body(64, title='Controlnet Processor Res'),
        controlnet_threshold_a: float = Body(64, title='Controlnet Threshold a'),
        controlnet_threshold_b: float = Body(64, title='Controlnet Threshold b'),
        controlnet_guidance: float = Body(1.0, title='ControlNet Guidance Strength'),
        controlnet_guessmode: bool = Body(True, title="Guess Mode"),
        seed: int = Body(-1, title="Seed"),
        subseed: int = Body(-1, title="Subseed"),
        subseed_strength: int = Body(-1, title="Subseed Strength"),
        sampler_index: str = Body("", title='Sampler Name'),
        batch_size: int = Body(1, title="Batch Size"),
        n_iter: int = Body(1, title="Iteration"),
        steps: int = Body(20, title="Steps"),
        cfg_scale: float = Body(7, title="CFG"),
        width: int = Body(512, title="width"),
        height: int = Body(512, title="height"),
        restore_faces: bool = Body(True, title="Restore Faces"),
        include_init_images: bool = Body(True, title="Include Init Images"),
        override_settings: Dict[str, Any] = Body(None, title="Override Settings"),
        override_settings_restore_afterwards: bool = Body(True, title="Restore Override Settings Afterwards"),    
        ):

        if mask:
            mask = decode_base64_to_image(mask)

        base64_decoded = base64.b64decode(init_images[0])
        image = Image.open(io.BytesIO(base64_decoded))
        image_np = np.array(image).astype('uint8')
        imgin = canny(image_np, controlnet_processor_res)
        # REGULAR IMAGE
        pil_img = Image.fromarray(imgin)
        # INVERT IMAGE
        # pil_img_in = Image.fromarray(imgin)
        # pil_img = PIL.ImageOps.invert(pil_img_in)
        buff = BytesIO()
        pil_img.save(buff, format="JPEG")
        new_image_string = base64.b64encode(buff.getvalue()).decode("utf-8")
        imgin3 = decode_base64_to_image(new_image_string)

        with queue_lock:
            p = StableDiffusionProcessingImg2Img(
                sd_model=shared.sd_model,
                outpath_samples=opts.outdir_samples or opts.outdir_img2img_samples,
                outpath_grids=opts.outdir_grids or opts.outdir_img2img_grids,
                prompt=prompt,
                negative_prompt=negative_prompt,
                init_images=[imgin3],
                styles=[],
                seed=seed,
                subseed=subseed,
                subseed_strength=subseed_strength,
                seed_resize_from_h=-1,
                seed_resize_from_w=-1,
                seed_enable_extras=False,
                sampler_name=sampler_index,
                batch_size=batch_size,
                n_iter=n_iter,
                steps=steps,
                cfg_scale=cfg_scale,
                width=width,
                height=height,
                restore_faces=restore_faces,
                tiling=False,
                mask=mask,
                mask_blur=mask_blur,
                inpainting_fill=inpainting_fill,
                resize_mode=resize_mode,
                denoising_strength=denoising_strength,
                inpaint_full_res=inpaint_full_res,
                inpaint_full_res_padding=inpaint_full_res_padding,
                inpainting_mask_invert=inpainting_mask_invert,
                override_settings=override_settings,
                do_not_save_samples=True,
                do_not_save_grid=True,
            )

            shared.state.begin()
            cn_image = Image.open(io.BytesIO(base64.b64decode(controlnet_input_image[0])))        
            cn_image_np = np.array(cn_image).astype('uint8')

            if controlnet_mask == [] :
                cn_mask_np = np.zeros(shape=(512, 512, 3)).astype('uint8')
            else:
                cn_mask = Image.open(io.BytesIO(base64.b64decode(controlnet_mask[0])))        
                cn_mask_np = np.array(cn_mask).astype('uint8')
        
            cn_args = {
                "control_net_enabled": True,
                "control_net_module": controlnet_module,
                "control_net_model": controlnet_model,
                "control_net_weight": controlnet_weight,
                "control_net_image": {'image': cn_image_np, 'mask': cn_mask_np},
                "control_net_scribble_mode": False,
                "control_net_resize_mode": controlnet_resize_mode,
                "control_net_rgbbgr_mode": False,
                "control_net_lowvram": controlnet_lowvram,
                "control_net_pres": controlnet_processor_res,
                "control_net_pthr_a": controlnet_threshold_a,
                "control_net_pthr_b": controlnet_threshold_b,
                "control_net_guidance_strength": controlnet_guidance,
                "control_net_guess_mode": controlnet_guessmode,
                "control_net_api_access": True,
            }

            p.scripts = scripts.scripts_img2img
            p.script_args = [0, ]
            for k, v in cn_args.items():
                setattr(p, k, v)

            if shared.cmd_opts.enable_console_prompts:
                print(f"\nimg2img: {prompt}", file=shared.progress_print_out)

            p.extra_generation_params["Mask blur"] = mask_blur

            processed = process_images(p)            
            p.close()

            generation_info_js = processed.js()
            if opts.samples_log_stdout:
                print(generation_info_js)

            if opts.do_not_show_images:
                processed.images = []
            shared.state.end()

        b64images = list(map(encode_to_base64, processed.images))
        return {"images": b64images, "info": processed.js()}
    
    @app.get("/controlnet/model_list")
    async def model_list():
        update_cn_models()
        print(list(cn_models_names.values()))
        return {"model_list": list(cn_models_names.values())}

    @app.post("/controlnet/detect")
    async def detect(
        controlnet_module: str = Body("None", title='Controlnet Module'),
        controlnet_input_images: List[str] = Body([], title='Controlnet Input Images'),
        controlnet_processor_res: int = Body(512, title='Controlnet Processor Resolution'),
        controlnet_threshold_a: float = Body(64, title='Controlnet Threshold a'),
        controlnet_threshold_b: float = Body(64, title='Controlnet Threshold b')
        ):

        available_modules = ["canny", 
                             "depth", 
                             "depth_leres", 
                             "fake_scribble", 
                             "hed", 
                             "mlsd", 
                             "normal_map", 
                             "openpose", 
                             "segmentation"]

        if controlnet_module not in available_modules:
            return {"images": [], "info": "Module not available"}
        if len(controlnet_input_images) == 0:
            return {"images": [], "info": "No image selected"}
        
        print(f"Detecting {str(len(controlnet_input_images))} images with the {controlnet_module} module.")

        results = []

        for input_image in controlnet_input_images:
            img = np.array(Image.open(io.BytesIO(base64.b64decode(input_image)))).astype('uint8')

            if controlnet_module == "canny":
                results.append(canny(img, controlnet_processor_res, controlnet_threshold_a, controlnet_threshold_b))
            elif controlnet_module == "hed":
                results.append(hed(img, controlnet_processor_res))
            elif controlnet_module == "mlsd":
                results.append(mlsd(img, controlnet_processor_res, controlnet_threshold_a, controlnet_threshold_b))
            elif controlnet_module == "depth":
                results.append(midas(img, controlnet_processor_res, np.pi * 2.0))
            elif controlnet_module == "normal_map":
                results.append(midas_normal(img, controlnet_processor_res, np.pi * 2.0, controlnet_threshold_a))
            elif controlnet_module == "depth_leres":
                results.append(leres(img, controlnet_processor_res, np.pi * 2.0, controlnet_threshold_a, controlnet_threshold_b))
            elif controlnet_module == "openpose":
                results.append(openpose(img, controlnet_processor_res, False))
            elif controlnet_module == "fake_scribble":
                results.append(fake_scribble(img, controlnet_processor_res))
            elif controlnet_module == "segmentation":
                results.append(uniformer(img, controlnet_processor_res))

        if controlnet_module == "hed":
            unload_hed()
        elif controlnet_module == "mlsd":
            unload_mlsd()
        elif controlnet_module == "depth" or controlnet_module == "normal_map":
            unload_midas()
        elif controlnet_module == "depth_leres":
            unload_leres()
        elif controlnet_module == "openpose":
            unload_openpose()
        elif controlnet_module == "segmentation":
            unload_uniformer()

        results64 = list(map(encode_to_base64, results))
        return {"images": results64, "info": "Success"}

try:
    import modules.script_callbacks as script_callbacks

    script_callbacks.on_app_started(controlnet_api)
except:
    pass

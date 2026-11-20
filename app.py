from __future__ import annotations
import base64, concurrent.futures, ctypes, json, os, platform, re, shutil, subprocess, sys, threading, time, uuid, copy
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from live_streams import LiveClipManager
from updater import GitHubUpdater

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('CLIPFORGE_HOME',Path.home()/'Videos'/'ClipForge'))
PROJECTS,EXPORTS=DATA/'projects',DATA/'exports'
WHISPER_DIR=DATA/'engine'/'python-packages'
for p in (PROJECTS,EXPORTS): p.mkdir(parents=True,exist_ok=True)
DEFAULTS={'mode':'local','ollama_url':'http://127.0.0.1:11434','ollama_model':'qwen3:4b-instruct','vision_model':'qwen3-vl:2b-instruct','provider':'openrouter','api_base':'https://openrouter.ai/api/v1','api_model':'openai/gpt-4o-mini','api_key_encrypted':'','storage_dir':str(DATA),'update_repo':'DARKUZAM/ClipForge','whisper_model':'base','clip_count':5,'duration_preset':'30-60','language':'auto','cpu_threads':4,'num_ctx':4096,'resolution':'1080','custom_width':1080,'custom_height':1920,'custom_min':20,'custom_max':60,'scene_frame_limit':72}
CONFIG=DATA/'config.json'; app=FastAPI(title='ClipForge Local',version='1.0.0')
pool=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix='clipforge'); JOBS={}; JOBS_LOCK=threading.Lock(); DLL_HANDLES=[]; DLL_DIRS=set()

def read_config():
 cfg=DEFAULTS.copy()
 try: cfg.update(json.loads(CONFIG.read_text('utf-8')))
 except (OSError,json.JSONDecodeError): pass
 return cfg
def write_config(cfg):
 CONFIG.parent.mkdir(parents=True,exist_ok=True); tmp=CONFIG.with_suffix('.tmp'); tmp.write_text(json.dumps(cfg,indent=2),'utf-8'); tmp.replace(CONFIG)
def protect_secret(value):
 if not value:return ''
 if os.name!='nt':return value
 class Blob(ctypes.Structure): _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_char))]
 import base64
 raw=value.encode(); buf=ctypes.create_string_buffer(raw); src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_char))); out=Blob()
 if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src),'ClipForge API key',None,None,None,0,ctypes.byref(out)): raise OSError('Windows could not protect the API key')
 try:return 'dpapi:'+base64.b64encode(ctypes.string_at(out.pbData,out.cbData)).decode()
 finally:ctypes.windll.kernel32.LocalFree(out.pbData)
def reveal_secret(value):
 if not value:return ''
 if not value.startswith('dpapi:') or os.name!='nt':return value
 class Blob(ctypes.Structure): _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_char))]
 import base64
 raw=base64.b64decode(value[6:]); buf=ctypes.create_string_buffer(raw); src=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_char))); out,desc=Blob(),ctypes.c_wchar_p()
 if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src),ctypes.byref(desc),None,None,None,0,ctypes.byref(out)):raise OSError('Saved API key cannot be decrypted for this Windows user')
 try:return ctypes.string_at(out.pbData,out.cbData).decode()
 finally:ctypes.windll.kernel32.LocalFree(out.pbData)
def safe_config():
 cfg=read_config(); cfg.pop('api_key_encrypted',None); cfg.pop('youtube_api_key_encrypted',None); cfg['has_api_key']=bool(read_config().get('api_key_encrypted')); return cfg
def load_whisper_path():
 global WHISPER_DIR
 WHISPER_DIR=DATA/'engine'/'python-packages'
 if WHISPER_DIR.exists() and str(WHISPER_DIR) not in sys.path:sys.path.insert(0,str(WHISPER_DIR))
 if os.name=='nt' and WHISPER_DIR.exists():
  for folder in {dll.parent for dll in WHISPER_DIR.rglob('*.dll')}:
   try:
    if str(folder).lower() not in DLL_DIRS:DLL_HANDLES.append(os.add_dll_directory(str(folder)));DLL_DIRS.add(str(folder).lower())
   except (OSError,AttributeError):pass
def now():return datetime.now(timezone.utc).isoformat()
def run(cmd,timeout=3600,cwd=None):
 try:p=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,cwd=cwd)
 except FileNotFoundError as e:raise RuntimeError(f'Required program not found: {cmd[0]}. Install it or add it to PATH.') from e
 if p.returncode:raise RuntimeError((p.stderr or p.stdout or f'Command failed: {cmd[0]}')[-1800:])
 return p
def tool_path(name):
 found=shutil.which(name)
 if found:return found
 bundled=ROOT/'tools'/(name+('.exe' if os.name=='nt' else ''))
 if bundled.exists():return str(bundled)
 raise RuntimeError(f'{name} is not installed or not on PATH')
def request_json(url,payload=None,headers=None,timeout=60):
 data=json.dumps(payload).encode() if payload is not None else None
 req=Request(url,data=data,headers={'Content-Type':'application/json',**(headers or {})},method='POST' if data is not None else 'GET')
 try:
  with urlopen(req,timeout=timeout) as resp:return json.loads(resp.read())
 except HTTPError as e:raise RuntimeError(f'AI service returned HTTP {e.code}: {e.read().decode("utf-8","replace")[-1200:]}') from e
 except (URLError,TimeoutError,OSError,json.JSONDecodeError) as e:raise RuntimeError(f'Could not connect to AI service: {e}') from e
def ollama_models(cfg):
 try:return [m.get('name','') for m in request_json(cfg['ollama_url'].rstrip('/')+'/api/tags',timeout=4).get('models',[]) if m.get('name')]
 except Exception:return []
def ai_complete(prompt,cfg,json_mode=False,images=None):
 if cfg.get('mode')=='api':
  key=reveal_secret(cfg.get('api_key_encrypted',''))
  if not key:raise RuntimeError('Add an API key in AI Settings before using API mode')
  base=cfg.get('api_base','https://openrouter.ai/api/v1').rstrip('/'); content=[{'type':'text','text':prompt}]
  for image_data in images or []:content.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+image_data}})
  payload={'model':cfg.get('api_model','openai/gpt-4o-mini'),'messages':[{'role':'user','content':content if images else prompt}],'temperature':.2}
  if json_mode:payload['response_format']={'type':'json_object'}
  result=request_json(base+'/chat/completions',payload,{'Authorization':'Bearer '+key,'HTTP-Referer':'http://localhost','X-Title':'ClipForge Local'},180)
  return result['choices'][0]['message']['content']
  
  
 payload={'model':cfg.get('vision_model','qwen3-vl:2b-instruct') if images else cfg.get('ollama_model','qwen3:4b-instruct'),'prompt':prompt,'stream':False,'keep_alive':0 if images else '5m','options':{'temperature':.15,'num_ctx':int(cfg.get('num_ctx',4096)),'num_predict':1800}}
 if images:payload['images']=images
 if json_mode:payload['format']='json'
 try:result=request_json(cfg.get('ollama_url','http://127.0.0.1:11434').rstrip('/')+'/api/generate',payload,timeout=600)
 except RuntimeError as e:
  if 'Could not connect' not in str(e):raise
  ollama=shutil.which('ollama')
  if not ollama:raise RuntimeError('Ollama is not reachable. Start Ollama or change its endpoint in AI Settings.') from e
  subprocess.Popen([ollama,'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)); time.sleep(2)
  result=request_json(cfg.get('ollama_url','http://127.0.0.1:11434').rstrip('/')+'/api/generate',payload,timeout=600)
 return str(result.get('response',''))
def parse_json_answer(text):
 match=re.search(r'\{.*\}',text,re.S)
 if not match:raise RuntimeError('The AI did not return a usable clip list. Try again or change the analysis model.')
 try:return json.loads(match.group(0))
 except json.JSONDecodeError as e:raise RuntimeError('The AI returned malformed clip analysis. Try fewer clips or a shorter project.') from e

live_clips=LiveClipManager(DATA,EXPORTS,lambda:tool_path('yt-dlp'),lambda:tool_path('ffmpeg'),read_config,ai_complete,parse_json_answer,lambda video,cfg:transcribe(video,cfg),lambda clip,transcript,path:make_ass(clip,transcript,path))
def project_file(pid):return PROJECTS/f'{pid}.json'
def load_project(pid):
 path=project_file(pid)
 if not path.exists():raise HTTPException(404,'Project not found')
 return json.loads(path.read_text('utf-8'))
def save_project(project):project['updated_at']=now(); project_file(project['id']).write_text(json.dumps(project,ensure_ascii=False,indent=2),'utf-8')
def list_projects():
 rows=[]
 for p in PROJECTS.glob('*.json'):
  try:
   x=json.loads(p.read_text('utf-8')); rows.append({'id':x['id'],'name':x['name'],'updated_at':x.get('updated_at'),'status':x.get('status','ready'),'clip_count':len(x.get('clips',[])),'source':x.get('source','')})
  except (OSError,ValueError,KeyError):pass
 return sorted(rows,key=lambda x:x.get('updated_at') or '',reverse=True)
def ffprobe(path):return json.loads(run([tool_path('ffprobe'),'-v','error','-show_format','-show_streams','-of','json',str(path)],60).stdout)
def transcribe(audio,cfg):
 load_whisper_path()
 try:from faster_whisper import WhisperModel
 except ImportError as e:raise RuntimeError('Local transcription is not installed. In AI Settings, install faster-whisper; the selected speech model downloads only when processing starts.') from e
 model=WhisperModel(cfg.get('whisper_model','base'),device='cpu',compute_type='int8',cpu_threads=max(1,int(cfg.get('cpu_threads',4))),num_workers=1,download_root=str(DATA/'models'/'whisper'))
 segs,_=model.transcribe(str(audio),language=None if cfg.get('language')=='auto' else cfg.get('language'),vad_filter=True,word_timestamps=True,beam_size=3)
 return [{'start':float(s.start),'end':float(s.end),'text':s.text.strip(),'words':[{'start':float(w.start),'end':float(w.end),'text':w.word.strip()} for w in (s.words or [])]} for s in segs]
def sample_scene_frames(video_path,project_dir,duration,cfg):
 try: import cv2
 except ImportError:return [],'unavailable: OpenCV is not installed'
 folder=project_dir/'scene-frames'; folder.mkdir(parents=True,exist_ok=True)
 limit=max(12,min(120,int(cfg.get('scene_frame_limit',72)))); total=min(limit,max(2,int(duration/1.5)+1)); step=duration/max(total-1,1)
 cap=cv2.VideoCapture(str(video_path))
 if not cap.isOpened():return [],'unavailable: could not read source video frames'
 frames=[]; previous=None
 try:
  for i in range(total):
   stamp=min(i*step,max(0.,duration-.05)); cap.set(cv2.CAP_PROP_POS_MSEC,stamp*1000); ok,image=cap.read()
   if not ok:continue
   h,w=image.shape[:2]; scale=min(1.,384/max(w,h)); small=cv2.resize(image,(max(1,int(w*scale)),max(1,int(h*scale))),interpolation=cv2.INTER_AREA) if scale<1 else image
   gray=cv2.cvtColor(cv2.resize(small,(96,54),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2GRAY)
   motion=0 if previous is None else min(100,int(float(cv2.absdiff(gray,previous).mean())*1.8)); previous=gray
   path=folder/f'frame_{i:03}.jpg'
   if cv2.imwrite(str(path),small,[int(cv2.IMWRITE_JPEG_QUALITY),58]):frames.append({'time':round(stamp,2),'motion':motion,'path':str(path)})
 finally:cap.release()
 return frames,('ready' if frames else 'unavailable: no decodable frames')
def visual_inputs(frames,start,end,limit=4):
 selected=[f for f in frames if start<=float(f.get('time',-1))<=end and Path(f.get('path','')).is_file()]
 if len(selected)>limit:
  spread=[]; width=max(.01,(end-start)/limit)
  for i in range(limit):
   left=start+i*width; right=end if i==limit-1 else start+(i+1)*width; choices=[f for f in selected if left<=float(f['time'])<=right]
   if choices:spread.append(max(choices,key=lambda f:int(f.get('motion',0))))
  selected=spread
 images=[]; labels=[]
 for frame in selected:
  try:images.append(base64.b64encode(Path(frame['path']).read_bytes()).decode('ascii')); labels.append(f"{float(frame['time']):.1f}s (motion {int(frame.get('motion',0))}/100)")
  except OSError:pass
 return images,labels
def analyze(transcript,duration,cfg,count,visual_frames=None):
 if not transcript and not visual_frames:raise RuntimeError('No speech transcript or readable scene frames were available to analyze.')
 ranges={'15-30':(15,30),'30-60':(30,60),'60-90':(60,90),'90-180':(90,180)}; lo,hi=ranges.get(cfg.get('duration_preset','30-60'),(20,60))
 if cfg.get('duration_preset')=='custom':lo,hi=int(cfg.get('custom_min',20)),int(cfg.get('custom_max',60))
 rows=[f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in transcript]; chunks=[rows[i:i+220] for i in range(0,len(rows),220)]; intervals=[(float(transcript[i]['start']),float(transcript[min(len(transcript)-1,i+219)]['end'])) for i in range(0,len(transcript),220)]
 if not chunks:
  windows=max(1,int((duration+59)//60)); intervals=[(duration*i/windows,duration*(i+1)/windows) for i in range(windows)]; chunks=[['(No speech transcript for this interval.)'] for _ in intervals]
 candidates=[]
 for chunk_index,chunk in enumerate(chunks):
  chunk_start,chunk_end=intervals[chunk_index]
  images,labels=visual_inputs(visual_frames or [],chunk_start,chunk_end)
  visual_note=('Frames in chronological order: '+', '.join(labels)+'. Motion is a coarse frame-difference estimate. Carefully inspect visible human faces and body language for genuine surprise/shock, smiles, laughter, excitement, sadness, anger, or fear. Only label emotion when it is visibly supported; use unknown when faces are absent, tiny, or unclear. Inspect action, movement and scene changes; do not infer unseen details.') if images else 'No frames available; use transcript evidence only.'
  prompt=f'''You are a funny, sharp short-form editor looking for the MOST entertaining moments in this video. Select up to {max(2,count*2)} self-contained clips. Prioritize laughs, playful banter, big reactions, surprising turns, wins or fails, chaotic energy, punchlines, satisfying reveals, and visible action like gameplay, escapes, chases, near misses, physical comedy and dramatic reactions. High motion is a clue, not proof a moment is good. Skip slow setup, dead air, repeated points, and context-dependent fragments. Do not claim internet popularity or invent a trend. Use natural sentence boundaries and obey duration {lo}-{hi} seconds. Source duration {duration:.1f}s. {visual_note} Return only JSON: {{"clips":[{{"start":0,"end":42,"title":"Short hook title","description":"One sentence","reason":"Why viewers would react or keep watching","visual_reason":"Visible action or movement","emotion_type":"surprise|laugh|smile|excitement|sadness|anger|fear|unknown","emotion_reason":"Visible facial/body-language evidence, or unknown","engagement":82,"hook":78,"emotion":55,"context":90,"fun":90,"energy":84,"movement":75,"shareability":88,"emoji":"😂","highlight_words":["wild","wait"]}}]}}. Scores 0-100. Emoji must be one relevant emoji. highlight_words are 1-4 actual spoken words. For speech, match transcript timestamps. Without speech, use supplied frame timestamps to estimate an engaging clip window. Transcript:\n'''+ ('\n'.join(chunk) if transcript else '(No speech transcript was detected.)')
  try:answer=parse_json_answer(ai_complete(prompt,cfg,True,images or None))
  except RuntimeError:
   if not images:raise
   answer=parse_json_answer(ai_complete(prompt,cfg,True))
  candidates.extend(answer.get('clips',[]))
 normalized=[]
 for c in candidates:
  try:
   start,end=max(0.,float(c['start'])),min(duration,float(c['end']))
   if end<=start:continue
   if end-start<lo:
    anchor=(start+end)/2; left=[float(s['start']) for s in transcript if float(s['start'])<=start+.8]; right=[float(s['end']) for s in transcript if float(s['end'])>=end-.8]
    options=[]
    for a in left:
     for b in right:
      if a<=anchor<=b and lo<=b-a<=hi:
       cost=abs(a-start)+abs(b-end)+abs((b-a)-(lo+hi)/2)*.12; options.append((cost,a,b))
    if options:_,start,end=min(options,key=lambda x:x[0])
    else:continue
   if end-start>hi:continue
   title=str(c.get('title','Untitled moment'))[:100]; description=str(c.get('description','')); reason=str(c.get('reason',''))
   highlights=c.get('highlight_words',[]); highlights=[highlights] if isinstance(highlights,str) else highlights if isinstance(highlights,list) else []
   highlights=[re.sub(r'[^\w\'-]','',str(w).lower()) for w in highlights[:4] if str(w).strip()]
   fun_context=f'{title} {description} {reason} ' + ' '.join(str(s.get('text','')) for s in transcript if start<=float(s['end']) and end>=float(s['start']))
   emoji_choices=['😂','😱','🔥','⚠️','🏃','🎮','🏆','💀','🤯','✨','👀','💥','🚨','💯','✅','❤️']
   suggested=str(c.get('emoji','')); emoji=next((e for e in emoji_choices if e in suggested),None)
   if not emoji:
    low=fun_context.lower()
    emoji={'surprise':'😱','laugh':'😂','smile':'😄','excitement':'🤩','sadness':'😢','anger':'😡','fear':'😨'}.get(emotion_type) or ('😂' if re.search(r'funny|laugh|joke|hilar|ridiculous|banter',low) else '🏃' if re.search(r'run|evacuat|escape|chase|fog zone',low) else '🎮' if re.search(r'game|gaming|player|boss|level|clutch',low) else '🏆' if re.search(r'win|won|victory|score|beat|champion',low) else '😱' if re.search(r'shock|surpris|unexpected|what\?',low) else '🔥')
   raw_engagement=int(c.get('engagement',65)); fun=int(c.get('fun',raw_engagement)); energy=int(c.get('energy',c.get('hook',60))); shareability=int(c.get('shareability',raw_engagement))
   ai_movement=int(c.get('movement',0)); motion_samples=[int(f.get('motion',0)) for f in (visual_frames or []) if start<=float(f.get('time',-1))<=end]; measured_motion=round(sum(motion_samples)/len(motion_samples)) if motion_samples else 0; movement=round(.55*ai_movement+.45*measured_motion) if motion_samples else ai_movement; emotion_type=str(c.get('emotion_type','unknown')).lower(); emotion_type=emotion_type if emotion_type in {'surprise','laugh','smile','excitement','sadness','anger','fear','unknown'} else 'unknown'; score=round(.22*raw_engagement+.22*fun+.10*energy+.10*shareability+.22*movement+.14*int(c.get('emotion',50)))
   normalized.append({'id':uuid.uuid4().hex[:10],'start':round(start,2),'end':round(end,2),'title':title,'description':description,'reason':reason,'visual_reason':str(c.get('visual_reason','')),'emotion_type':emotion_type,'emotion_reason':str(c.get('emotion_reason','')),'engagement':score,'hook':int(c.get('hook',60)),'emotion':int(c.get('emotion',50)),'context':int(c.get('context',70)),'fun':fun,'energy':energy,'movement':movement,'shareability':shareability,'emoji':emoji,'highlight_words':highlights,'aspect':'9:16','caption_style':'viral','crop':'center','subtitles':True,'accepted':True})
  except (ValueError,TypeError,KeyError):continue
 normalized.sort(key=lambda c:c['engagement'],reverse=True); selected=[]
 for c in normalized:
  if all(min(c['end'],x['end'])-max(c['start'],x['start'])<min(c['end']-c['start'],x['end']-x['start'])*.65 for x in selected):selected.append(c)
  if len(selected)>=count:break
 if not selected:raise RuntimeError('No viable clips returned. Adjust duration range or analyze again.')
 return selected

def ass_time(seconds):
 cs=max(0,int(seconds*100+.5)); h,rem=divmod(cs,360000); m,rem=divmod(rem,6000); s,cent=divmod(rem,100); return f'{h}:{m:02}:{s:02}.{cent:02}'
def ass_color(color):
 h=color.lstrip('#')
 if len(h)!=6:h='FFFFFF'
 return f'&H00{h[4:6]}{h[2:4]}{h[0:2]}'
def caption_emoji(text,default='✨'):
 low=text.lower()
 if re.search(r'funny|laugh|joke|hilar|ridiculous|fail|oops',low):return '😂'
 if re.search(r'run|evacuat|escape|chase|danger|fog zone|closing in',low):return '🏃'
 if re.search(r'game|gaming|player|boss|level|clutch',low):return '🎮'
 if re.search(r'win|won|victory|score|beat|champion',low):return '🏆'
 if re.search(r'what|shock|surpris|unexpected|no way',low):return '😱'
 return default if default in {'😂','😱','🔥','⚠️','🏃','🎮','🏆','💀','🤯','✨','👀','💥','🚨','💯','✅','❤️'} else '🔥'
def make_ass(clip,transcript,output):
 style=clip.get('caption_style','viral'); styles={'viral':('Arial',64,'FFFFFF','000000',4,2,1),'bold':('Arial',58,'FFFFFF','000000',4,3,1),'minimal':('Arial',42,'FFFFFF','000000',2,1,1),'cinematic':('Georgia',42,'FFFFFF','000000',2,1,0),'karaoke':('Arial',54,'FFFFFF','000000',4,2,1),'podcast':('Arial',52,'FFFFFF','131313',4,2,1),'gaming':('Arial',58,'FFFFFF','000000',4,3,1)}
 font,size,primary,outline,shadow,bold,italic=styles.get(style,styles['viral']); font=re.sub(r'[,\r\n]','',str(clip.get('caption_font',font)))[:48] or 'Arial'; size=max(28,min(110,int(clip.get('caption_size',size)))); primary=ass_color(clip.get('text_color',primary)); outline=ass_color(clip.get('accent_color',outline)); highlight=ass_color(clip.get('highlight_color','#ff302d')); emoji_size=max(28,min(120,int(clip.get('emoji_size',60)))); emoji_position=max(8,min(85,int(clip.get('emoji_position',max(8,int(clip.get('caption_position',72))-15))))); emoji_xpos=round(1080*max(8,min(92,int(clip.get('emoji_x',50))))/100); words_per_caption=max(1,min(8,int(clip.get('caption_words',4)))); position=max(20,min(86,int(clip.get('caption_position',72)))); pos_y=round(1920*position/100); pos_x=round(1080*max(8,min(92,int(clip.get('caption_x',50))))/100); overlay_y=round(1920*max(8,min(55,int(clip.get('overlay_position',19))))/100); emoji_y=round(1920*emoji_position/100); caption_bg=clip.get('caption_background','none'); border_style=3 if caption_bg=='box' else 1; back=ass_color(clip.get('background_color','#080808')) if caption_bg=='box' else '&HAA000000'; caption_outline=back if caption_bg=='box' else outline
 lines=['[Script Info]','ScriptType: v4.00+','PlayResX: 1080','PlayResY: 1920','WrapStyle: 2','ScaledBorderAndShadow: yes','','[V4+ Styles]','Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding',f'Style: Caption,{font},{size},{primary},&H000000FF,{caption_outline},{back},{bold},{italic},0,0,100,100,0,0,{border_style},4,{shadow},5,60,60,0,1','Style: Hook,Arial,46,&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,13,0,8,44,44,100,1','','[Events]','Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text']
 if style=='viral' and clip.get('title'):
  hook=r'{\an8\pos(540,260)}'+re.sub(r'[{}]','',clip['title'])
  lines.append(f"Dialogue: 1,0:00:00.00,{ass_time(min(2.8,max(.8,clip['end']-clip['start'])))},Hook,,0,0,0,,{hook}")
 if clip.get('custom_text'):
  custom=re.sub(r'[{}]','',str(clip['custom_text']))[:500]; custom_size=max(24,min(100,int(clip.get('overlay_size',48)))); custom_color=ass_color(clip.get('overlay_color',clip.get('text_color','#ffffff')))
  lines.append(f"Dialogue: 2,0:00:00.00,{ass_time(clip['end']-clip['start'])},Hook,,0,0,0,,{{\\an5\\pos(540,{overlay_y})\\fn{font}\\fs{custom_size}\\c{custom_color}}}{custom}")
 highlights=set(clip.get('highlight_words',[])); emphasis={'run','wait','look','no','stop','go','wow','what','why','how','crazy','wild','insane','never','again','secret','free','win','won','lost','fail','oops','clutch','evacuate','escape','danger','fog','closing','boom','really','literally','actually'}
 for seg in transcript:
  if seg['end']<clip['start'] or seg['start']>clip['end']:continue
  words=[w for w in seg.get('words',[]) if clip['start']<=w['start']<clip['end']]
  if words and style=='viral':
   groups=[]; group=[]
   for word in words:
    group.append(word)
    if len(group)>=words_per_caption or re.search(r'[.!?]["\']?$',word['text']):groups.append(group);group=[]
   if group:groups.append(group)
   for group in groups:
    tokens=[re.sub(r"[^\w'-]",'',w['text'].lower()) for w in group]
    focus=next((i for i,t in enumerate(tokens) if t and t in highlights),None)
    if focus is None:focus=next((i for i,t in enumerate(tokens) if t and t in emphasis),None)
    clean=[re.sub(r'[{}]','',w['text'].strip()) for w in group]
    phrase=' '.join(clean)
    for active_i,word in enumerate(group):
     start=max(clip['start'],float(word['start']))
     next_start=float(group[active_i+1]['start']) if active_i+1<len(group) else float(word['end'])
     end=min(clip['end'],max(start+.06,next_start))
     parts=[]
     for i,token in enumerate(clean):
      if i==active_i:parts.append('{\\c'+highlight+'\\3c'+outline+'\\bord5\\4c'+highlight+'\\shad2\\blur1\\fscx108\\fscy108\\b1}'+token+r'{\rCaption}')
      else:parts.append(token)
     caption=' '.join(parts)
     lines.append(f"Dialogue: 0,{ass_time(start-clip['start'])},{ass_time(end-clip['start'])},Caption,,0,0,0,,{{\\an5\\pos({pos_x},{pos_y})}}{caption}")
    if focus is not None:
     emo=clip.get('emoji_override') or caption_emoji(phrase,clip.get('emoji','✨')); emo_start=max(clip['start'],float(group[0]['start']))-clip['start']; emo_end=min(clip['end'],max(float(group[-1]['end']),float(group[0]['start'])+.55))-clip['start']; anim=f'{{\\an5\\pos({emoji_xpos},{emoji_y})\\fnSegoe UI Emoji\\fs{emoji_size}\\bord2\\3c&H00FFFFFF&\\4c{highlight}\\shad3\\fad(50,100)\\fscx55\\fscy55\\t(0,150,\\fscx120\\fscy120)\\t(150,300,\\fscx100\\fscy100)}}{emo}'
     lines.append(f"Dialogue: 2,{ass_time(emo_start)},{ass_time(emo_end)},Caption,,0,0,0,,{anim}")
  elif words and style=='karaoke':
   for w in words:
    start,end=max(clip['start'],w['start']),min(clip['end'],w['end']); text=re.sub(r'[{}]','',w['text']); lines.append(f"Dialogue: 0,{ass_time(start-clip['start'])},{ass_time(max(start+.12,end)-clip['start'])},Caption,,0,0,0,,{{\\an5\\pos({pos_x},{pos_y})\\c{highlight}\\b1}}{text}")
  else:
   text=re.sub(r'[{}]','',seg['text'])
   if text:lines.append(f"Dialogue: 0,{ass_time(max(seg['start'],clip['start'])-clip['start'])},{ass_time(min(seg['end'],clip['end'])-clip['start'])},Caption,,0,0,0,,{{\\an5\\pos({pos_x},{pos_y})}}{text}")
 output.write_text('\n'.join(lines),encoding='utf-8-sig')
def render_clip(project,clip,cfg,index,total,job_id):
 source=Path(project['video_path']); name=re.sub(r'[^a-zA-Z0-9_-]+','-',clip['title']).strip('-')[:48] or f'clip-{index}'; target_dir=EXPORTS/project['id']; target_dir.mkdir(parents=True,exist_ok=True); target=target_dir/f'{index:02d}-{name}.mp4'
 aspect=clip.get('aspect','9:16'); dimensions={'9:16':(1080,1920),'16:9':(1920,1080),'1:1':(1080,1080),'4:5':(1080,1350)}; w,h=dimensions.get(aspect,dimensions['9:16'])
 if cfg.get('resolution')=='720':w,h=round(w*2/3),round(h*2/3)
 elif cfg.get('resolution')=='custom':w,h=int(cfg.get('custom_width',w)),int(cfg.get('custom_height',h))
 w=max(2,w//2*2);h=max(2,h//2*2)
 crop=clip.get('crop','center')
 vf=f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}'
 if crop=='fit':vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black'
 elif aspect=='original' or crop=='original':vf='scale=trunc(iw/2)*2:trunc(ih/2)*2'
 ass_file=target.with_suffix('.ass')
 if clip.get('subtitles',True):
  make_ass(clip,project.get('transcript',[]),ass_file); escaped=str(ass_file).replace('\\','/').replace(':','\\:').replace("'","\\'"); vf+=f",ass='{escaped}'"
 cmd=[tool_path('ffmpeg'),'-y','-loglevel','error','-ss',f"{clip['start']:.3f}",'-i',str(source),'-t',f"{max(.1,clip['end']-clip['start']):.3f}",'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','20',*(['-an'] if clip.get('muted') else ['-c:a','aac','-b:a','192k']),'-movflags','+faststart','-progress','pipe:1','-nostats',str(target)]
 p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1)
 clip_duration=max(.1,clip['end']-clip['start'])
 for line in p.stdout:
  if line.startswith('out_time_ms='):
   try:
    fraction=min(1.,int(line.split('=',1)[1])/1_000_000/clip_duration)
    with JOBS_LOCK:JOBS[job_id]['progress']=82+int(17*(index-1+fraction)/max(total,1))
   except ValueError:pass
 err=p.stderr.read() if p.stderr else ''; p.wait()
 if p.returncode:raise RuntimeError(f"FFmpeg could not render {clip['title']}: {err[-1500:]}")
 clip['export_path']=str(target); clip['subtitle_path']=str(ass_file) if ass_file.exists() else None
 srt=target.with_suffix('.srt'); entries=[]
 for seg in project.get('transcript',[]):
  if seg['end']<clip['start'] or seg['start']>clip['end']:continue
  start,end=max(seg['start'],clip['start'])-clip['start'],min(seg['end'],clip['end'])-clip['start']
  def stamp(t):
   ms=max(0,int(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); sec,ms=divmod(ms,1000); return f'{h:02}:{m:02}:{sec:02},{ms:03}'
  entries.append(f'{len(entries)+1}\n{stamp(start)} --> {stamp(end)}\n{seg["text"]}')
 srt.write_text('\n\n'.join(entries)+'\n','utf-8'); clip['srt_path']=str(srt)
def run_job(job_id,pid,source,name,options):
 def stage(label,progress):
  with JOBS_LOCK:JOBS[job_id].update(stage=label,progress=progress)
 try:
  cfg=read_config(); cfg.update(options); stage('Preparing source',2); project_dir=PROJECTS/pid; project_dir.mkdir(parents=True,exist_ok=True)
  if source.startswith(('http://','https://')):
   if urlparse(source).scheme not in ('http','https'):raise RuntimeError('Only HTTP and HTTPS video URLs are supported')
   stage('Downloading video',5); run([tool_path('yt-dlp'),'--no-playlist','--merge-output-format','mp4','-f','bv*[height<=1080]+ba/b[height<=1080]/b','-o',str(project_dir/'source.%(ext)s'),source],1800)
   videos=[x for x in project_dir.glob('source.*') if x.suffix.lower() in {'.mp4','.mkv','.webm','.mov'}]
   if not videos:raise RuntimeError('yt-dlp did not create a supported video file')
   source_path=videos[0]
  else:
   incoming=Path(source).resolve()
   if not incoming.is_file():raise RuntimeError('The selected video file could not be found')
   if incoming.suffix.lower() not in {'.mp4','.mkv','.mov','.webm','.avi','.m4v'}:raise RuntimeError(f'Unsupported video format: {incoming.suffix}')
   saved=project_dir/('source'+incoming.suffix.lower())
   if incoming!=saved and not saved.exists():shutil.copy2(incoming,saved)
   source_path=saved if saved.exists() else incoming
  stage('Inspecting video',12); metadata=ffprobe(source_path); duration=float(metadata.get('format',{}).get('duration',0))
  if duration<=0:raise RuntimeError('Could not read video duration')
  audio=project_dir/'audio-16k.wav'; stage('Extracting audio',18); run([tool_path('ffmpeg'),'-y','-i',str(source_path),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)],900)
  stage('Transcribing locally',24); transcript=transcribe(audio,cfg); (project_dir/'transcript.json').write_text(json.dumps(transcript,ensure_ascii=False,indent=2),'utf-8')
  stage('Sampling scenes & movement',42); visual_frames,visual_status=sample_scene_frames(source_path,project_dir,duration,cfg)
  project={'id':pid,'name':name or source_path.stem,'source':source,'video_path':str(source_path),'duration':duration,'metadata':metadata,'transcript':transcript,'visual_frames':visual_frames,'visual_status':visual_status,'clips':[],'created_at':now(),'status':'analyzing'}; save_project(project)
  stage('Analyzing scenes with AI',50); clips=analyze(transcript,duration,cfg,int(cfg.get('clip_count',5)),visual_frames)
  project.update(clips=clips,status='ready'); save_project(project); stage('Rendering clips',80)
  accepted=[c for c in clips if c.get('accepted',True)]
  for i,clip in enumerate(accepted,1):render_clip(project,clip,cfg,i,len(accepted),job_id)
  save_project(project); stage('Finished',100)
  with JOBS_LOCK:JOBS[job_id].update(status='finished',project_id=pid,error=None)
 except Exception as e:
  with JOBS_LOCK:JOBS[job_id].update(status='error',stage='Needs attention',error=str(e))
  try:
   path=project_file(pid)
   if path.exists():project=json.loads(path.read_text('utf-8')); project.update(status='error',error=str(e)); save_project(project)
  except Exception:pass

class ConfigUpdate(BaseModel):values:dict[str,Any]
class ConnectionTest(BaseModel):mode:str|None=None
class JobCreate(BaseModel):source:str; name:str=''; options:dict[str,Any]={}
class LiveStart(BaseModel):url:str; mode:str='local'; pre_seconds:int=12; post_seconds:int=18; clip_count:int=8; layout:str='auto'; caption_font:str='Arial'; caption_size:int=64; text_color:str='#ffffff'; accent_color:str='#ffffff'; highlight_color:str='#ff302d'; caption_words:int=4; caption_position:int=72; caption_x:int=50; emoji_size:int=60; emoji_position:int=57; emoji_x:int=50; emoji_override:str=''; caption_background:str='none'; background_color:str='#080808'
class ClipUpdate(BaseModel):changes:dict[str,Any]

@app.get('/api/health')
def health():
 cfg=read_config()
 load_whisper_path()
 try:import faster_whisper; whisper=True
 except ImportError:whisper=False
 return {'ok':True,'ollama_models':ollama_models(cfg),'ffmpeg':bool(shutil.which('ffmpeg')),'yt_dlp':bool(shutil.which('yt-dlp')),'whisper_installed':whisper}
@app.get('/api/config')
def config_get():return safe_config()
@app.put('/api/config')
def config_put(body:ConfigUpdate):
 cfg=read_config(); allowed=('mode','ollama_url','ollama_model','vision_model','scene_frame_limit','provider','api_base','api_model','storage_dir','update_repo','whisper_model','clip_count','duration_preset','language','cpu_threads','num_ctx','custom_min','custom_max','resolution','custom_width','custom_height')
 for key in allowed:
  if key in body.values:cfg[key]=body.values[key]
 if body.values.get('api_key'):cfg['api_key_encrypted']=protect_secret(str(body.values['api_key']))
 if body.values.get('clear_api_key'):cfg['api_key_encrypted']=''
 write_config(cfg);return safe_config()
@app.post('/api/choose-storage')
def choose_storage():
 global DATA,PROJECTS,EXPORTS,CONFIG
 try:
  import tkinter as tk
  from tkinter import filedialog
  root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True)
  selected=filedialog.askdirectory(title='Choose ClipForge project storage',initialdir=str(DATA)); root.destroy()
 except Exception as e:raise HTTPException(500,f'Could not open the Windows folder picker: {e}')
 if not selected:return {'cancelled':True,'storage_dir':str(DATA)}
 target=Path(selected).resolve()
 if target==DATA.resolve():return {'cancelled':False,'storage_dir':str(DATA)}
 if DATA.resolve() in target.parents:raise HTTPException(400,'Choose a storage folder outside the current ClipForge folder')
 target.mkdir(parents=True,exist_ok=True)
 folders=('projects','exports','imports','models','engine'); conflicts=[]
 for folder in folders:
  old,new=DATA/folder,target/folder
  if old.exists() and new.exists():conflicts.extend(str(item.name) for item in old.iterdir() if (new/item.name).exists())
 old_config=CONFIG; new_config=target/'config.json'
 if old_config.exists() and old_config.resolve()!=new_config.resolve() and new_config.exists():conflicts.append('config.json')
 if conflicts:raise HTTPException(409,'The selected folder already contains conflicting ClipForge items: '+', '.join(conflicts[:8]))
 for folder in folders:
  old,new=DATA/folder,target/folder
  if old.exists():
   if new.exists():
    new.mkdir(parents=True,exist_ok=True)
    for item in old.iterdir():shutil.move(str(item),str(new/item.name))
    old.rmdir()
   else:shutil.move(str(old),str(new))
 if old_config.exists() and old_config.resolve()!=new_config.resolve():shutil.copy2(old_config,new_config)
 DATA,PROJECTS,EXPORTS,CONFIG=target,target/'projects',target/'exports',new_config
 cfg=read_config(); cfg['storage_dir']=str(target); write_config(cfg)
 return {'cancelled':False,'storage_dir':str(DATA)}
@app.post('/api/test-connection')
def test_connection(body:ConnectionTest):
 cfg=read_config()
 if body.mode:cfg['mode']=body.mode
 return {'ok':True,'message':ai_complete('Reply with only the word connected.',cfg)[:100].strip()}
@app.post('/api/install-transcription')
def install_transcription():
 try:
  WHISPER_DIR.mkdir(parents=True,exist_ok=True)
  run([sys.executable,'-m','pip','install','--upgrade','--target',str(WHISPER_DIR),'faster-whisper','av>=11,<19'],1800)
  load_whisper_path()
 except RuntimeError as e:raise HTTPException(500,str(e))
 return {'ok':True,'message':'Local speech recognition installed inside ClipForge. The speech model downloads when processing your first video.'}
@app.get('/api/system')
def system_info():
 gpu='GPU unavailable'; cpu=platform.processor() or 'CPU detected by Windows'; ram_gb=None; cores=os.cpu_count() or 1
 if sys.platform=='win32':
  try:
   import winreg
   key=winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0'); cpu=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip(); winreg.CloseKey(key)
  except Exception:pass
  class MemoryStatus(ctypes.Structure):_fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),('ullTotalPhys',ctypes.c_ulonglong),('ullAvailPhys',ctypes.c_ulonglong),('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),('ullAvailExtendedVirtual',ctypes.c_ulonglong)]
  try:
   memory=MemoryStatus(); memory.dwLength=ctypes.sizeof(memory); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)); ram_gb=round(memory.ullTotalPhys/1024**3,1)
  except Exception:pass
  try:gpu=subprocess.check_output(['powershell','-NoProfile','-Command','(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) $([math]::Round($_.AdapterRAM/1GB,1)) GB" }) -join "; "'],text=True,stderr=subprocess.DEVNULL,timeout=7).strip() or gpu
  except Exception:pass
 try:gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total,memory.free','--format=csv,noheader'],text=True,stderr=subprocess.DEVNULL,timeout=4).strip() or gpu
 except Exception:pass
 return {'cpu':f'{cpu} · {cores} logical threads','cpu_name':cpu,'cores':cores,'ram_gb':ram_gb,'gpu':gpu,'free_disk_gb':round(shutil.disk_usage(DATA).free/1024**3,1),'storage_dir':str(DATA),'ollama_models':ollama_models(read_config()),'ffmpeg':bool(shutil.which('ffmpeg')),'yt_dlp':bool(shutil.which('yt-dlp'))}
@app.post('/api/live/start')
def live_start(body:LiveStart):
 live_clips.data_dir=DATA; live_clips.exports_dir=EXPORTS
 try:sid=live_clips.start(body.model_dump())
 except ValueError as e:raise HTTPException(400,str(e))
 return {'session_id':sid}
@app.get('/api/live/{sid}')
def live_status(sid:str):
 try:return live_clips.public(sid)
 except KeyError:raise HTTPException(404,'Live clip session not found')
@app.post('/api/live/{sid}/stop')
def live_stop(sid:str):
 try:return live_clips.stop(sid)
 except KeyError:raise HTTPException(404,'Live clip session not found')
@app.get('/api/update/check')
def update_check():
 try:return GitHubUpdater(ROOT,DATA,read_config).check()
 except ValueError as e:raise HTTPException(400,str(e))
 except RuntimeError as e:raise HTTPException(502,str(e))
@app.post('/api/update/install')
def update_install():
 try:return GitHubUpdater(ROOT,DATA,read_config).apply()
 except ValueError as e:raise HTTPException(400,str(e))
 except RuntimeError as e:raise HTTPException(502,str(e))
@app.post('/api/jobs')
def create_job(body:JobCreate):
 if not body.source.strip():raise HTTPException(400,'Choose a video file or paste a URL')
 if body.source.startswith(('http://','https://')) and not shutil.which('yt-dlp'):raise HTTPException(400,'yt-dlp is required for video URLs')
 pid,jid=uuid.uuid4().hex[:12],uuid.uuid4().hex[:12]
 with JOBS_LOCK:JOBS[jid]={'id':jid,'project_id':pid,'status':'running','stage':'Queued','progress':0,'error':None,'created_at':now()}
 pool.submit(run_job,jid,pid,body.source.strip(),body.name.strip(),body.options);return {'job_id':jid,'project_id':pid}
@app.post('/api/upload')
def upload_video(file:UploadFile=File(...)):
 ext=Path(file.filename or 'video').suffix.lower()
 if ext not in {'.mp4','.mkv','.mov','.webm','.avi','.m4v'}:raise HTTPException(400,'Select an MP4, MKV, MOV, WebM, AVI, or M4V video')
 uid=uuid.uuid4().hex[:12];target=DATA/'imports'/f'{uid}{ext}';target.parent.mkdir(parents=True,exist_ok=True);size=0
 with target.open('wb') as f:
  while chunk:=file.file.read(1024*1024):
   size+=len(chunk)
   if size>20*1024**3:target.unlink(missing_ok=True);raise HTTPException(413,'Video exceeds the 20 GB import limit')
   f.write(chunk)
 return {'path':str(target),'name':Path(file.filename or 'Video').stem}
@app.get('/api/jobs/{jid}')
def get_job(jid):
 with JOBS_LOCK:
  if jid not in JOBS:raise HTTPException(404,'Job not found')
  return dict(JOBS[jid])
@app.get('/api/projects')
def projects_get():return list_projects()
@app.get('/api/projects/{pid}')
def project_get(pid):return load_project(pid)
@app.delete('/api/projects/{pid}')
def project_delete(pid):
 load_project(pid)
 for path in (PROJECTS/pid,EXPORTS/pid):
  if path.exists():shutil.rmtree(path)
 project_file(pid).unlink(missing_ok=True);return {'ok':True}
@app.put('/api/projects/{pid}/clips/{cid}')
def clip_update(pid,cid,body:ClipUpdate):
 project=load_project(pid);clip=next((c for c in project.get('clips',[]) if c['id']==cid),None)
 if not clip:raise HTTPException(404,'Clip not found')
 allowed={'start','end','title','description','reason','aspect','caption_style','crop','subtitles','accepted','text_color','accent_color','highlight_color','caption_font','caption_size','caption_words','caption_position','caption_x','overlay_position','caption_background','background_color','emoji_size','emoji_position','emoji_x','custom_text','overlay_size','overlay_color','crop','muted'}
 for k,v in body.changes.items():
  if k in allowed:clip[k]=v
 render_changes={'start','end','title','aspect','caption_style','crop','subtitles','text_color','accent_color','highlight_color','caption_font','caption_size','caption_words','caption_position','caption_x','overlay_position','caption_background','background_color','emoji_size','emoji_position','emoji_x','custom_text','overlay_size','overlay_color','muted'}
 if render_changes.intersection(body.changes):
  for key in ('export_path','subtitle_path','srt_path'):clip.pop(key,None)
 if float(clip['start'])<0 or float(clip['end'])>float(project['duration']) or float(clip['end'])<=float(clip['start']):raise HTTPException(400,'Clip boundaries must be valid and within the video')
 save_project(project);return clip
@app.post('/api/projects/{pid}/clips/{cid}/split')
def clip_split(pid,cid,body:ClipUpdate):
 project=load_project(pid); clips=project.get('clips',[]); index=next((i for i,c in enumerate(clips) if c['id']==cid),None)
 if index is None:raise HTTPException(404,'Clip not found')
 clip=clips[index]
 try:cut=float(body.changes.get('time',0))
 except (TypeError,ValueError):raise HTTPException(400,'Split time must be a number')
 if not float(clip['start'])+.2<cut<float(clip['end'])-.2:raise HTTPException(400,'The playhead must be inside the clip to split it')
 left,right=copy.deepcopy(clip),copy.deepcopy(clip)
 for part,item in enumerate((left,right),1):
  item['id']=uuid.uuid4().hex[:10]; item['title']=str(clip.get('title','Clip'))+f' · {part}'; item.pop('export_path',None); item.pop('subtitle_path',None); item.pop('srt_path',None)
 left['end']=round(cut,2); right['start']=round(cut,2); clips[index:index+1]=[left,right]; save_project(project); return {'clips':[left,right],'project':project}
@app.put('/api/projects/{pid}/transcript/{index}')
def transcript_update(pid,index:int,body:ClipUpdate):
 project=load_project(pid)
 if index<0 or index>=len(project.get('transcript',[])):raise HTTPException(404,'Subtitle segment not found')
 segment=project['transcript'][index]
 previous_start,previous_end=float(segment['start']),float(segment['end'])
 for key in ('text','start','end'):
  if key in body.changes:segment[key]=body.changes[key]
 if not str(segment.get('text','')).strip() or float(segment['start'])<0 or float(segment['end'])>float(project['duration']) or float(segment['end'])<=float(segment['start']):raise HTTPException(400,'Subtitle text and timings must be valid and within the source video')
 if ('start' in body.changes or 'end' in body.changes) and segment.get('words'):
  new_start,new_end=float(segment['start']),float(segment['end']); scale=(new_end-new_start)/max(.001,previous_end-previous_start)
  for word in segment['words']:
   word['start']=new_start+(float(word['start'])-previous_start)*scale; word['end']=new_start+(float(word['end'])-previous_start)*scale
 for clip in project.get('clips',[]):
  if float(clip['start'])<=max(float(segment['end']),previous_end) and float(clip['end'])>=min(float(segment['start']),previous_start):
   for key in ('export_path','subtitle_path','srt_path'):clip.pop(key,None)
 save_project(project); (PROJECTS/pid/'transcript.json').write_text(json.dumps(project['transcript'],ensure_ascii=False,indent=2),'utf-8');return segment
@app.post('/api/projects/{pid}/reanalyze')
def reanalyze_project(pid,body:JobCreate):
 project=load_project(pid)
 if not project.get('transcript'):raise HTTPException(400,'This project has no saved transcript to analyze')
 jid=uuid.uuid4().hex[:12]
 with JOBS_LOCK:JOBS[jid]={'id':jid,'project_id':pid,'status':'running','stage':'Analyzing existing transcript','progress':50,'created_at':now()}
 def do_reanalyze():
  try:
   cfg=read_config(); cfg.update(body.options); clips=analyze(project['transcript'],float(project['duration']),cfg,int(cfg.get('clip_count',len(project.get('clips',[])) or 5)),project.get('visual_frames',[]))
   project.update(clips=clips,status='ready'); save_project(project)
   selected=[c for c in clips if c.get('accepted',True)]
   for i,c in enumerate(selected,1):render_clip(project,c,cfg,i,len(selected),jid)
   save_project(project)
   with JOBS_LOCK:JOBS[jid].update(status='finished',stage='Finished',progress=100)
  except Exception as e:
   with JOBS_LOCK:JOBS[jid].update(status='error',stage='Needs attention',error=str(e))
 pool.submit(do_reanalyze);return {'job_id':jid}
@app.post('/api/projects/{pid}/export')
def export_project(pid):
 project=load_project(pid);selected=[c for c in project.get('clips',[]) if c.get('accepted',True)]
 if not selected:raise HTTPException(400,'Select at least one clip to export')
 jid=uuid.uuid4().hex[:12]
 with JOBS_LOCK:JOBS[jid]={'id':jid,'project_id':pid,'status':'running','stage':'Rendering clips','progress':0,'created_at':now()}
 def do_export():
  try:
   for i,c in enumerate(selected,1):render_clip(project,c,read_config(),i,len(selected),jid)
   save_project(project)
   with JOBS_LOCK:JOBS[jid].update(status='finished',stage='Finished',progress=100)
  except Exception as e:
   with JOBS_LOCK:JOBS[jid].update(status='error',stage='Needs attention',error=str(e))
 pool.submit(do_export);return {'job_id':jid}
@app.get('/api/file')
def get_file(path:str):
 c=Path(path).resolve();roots=[PROJECTS.resolve(),EXPORTS.resolve(),(DATA/'imports').resolve()]
 if not any(c==r or r in c.parents for r in roots) or not c.is_file():raise HTTPException(404,'File not found')
 return FileResponse(c)
@app.get('/')
def home():return FileResponse(ROOT/'static'/'index.html')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

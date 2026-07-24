#!/usr/bin/env python3
"""Linux 企业级 SSD 稳定性与耐久压力测试系统。

默认演示模式绝不写盘。真实裸盘 fio 压测必须同时满足：Linux、root、
ENABLE_DESTRUCTIVE_FIO=1，以及 SSD 准入检查通过。
"""
import json
import io
import os
import platform
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT, DATA_FILE = Path(__file__).parent, Path(__file__).parent / "data.json"
LOG_ROOT = ROOT / "logs"
LOCK, PROCESSES = threading.Lock(), {}
DEFAULT_PLANS = [
    {"id":"plan-burnin","name":"72 小时耐久老化","duration":72,"block_size":"4K","read_ratio":30,"queue_depth":64,"threshold_temp":70,"description":"随机混合 I/O，验证企业盘长时写入稳定性、温度节流与尾延迟"},
    {"id":"plan-stability","name":"24 小时稳定性验证","duration":24,"block_size":"128K","read_ratio":50,"queue_depth":32,"threshold_temp":65,"description":"平衡读写负载，适用于到货验收、批量抽检"},
    {"id":"plan-spike","name":"突发负载恢复测试","duration":8,"block_size":"4K","read_ratio":20,"queue_depth":128,"threshold_temp":72,"description":"高队列深度脉冲压力，关注延迟尖峰与恢复能力"},
]
BLOCK_SIZES={"4K","8K","16K","32K","64K","128K","256K","1M"}
IO_PATTERNS={"randrw","randread","randwrite","read","write"}
VERIFY_MODES={"none","crc32c"}
EXTRA_OPTION_RULES={
    "thinktime":(0,1000000), "thinktime_blocks":(1,100000),
    "iodepth_batch_submit":(1,1024), "iodepth_batch_complete_min":(1,1024),
    "iodepth_batch_complete_max":(1,1024), "norandommap":(0,1), "refill_buffers":(0,1),
}
RANDOM_GENERATORS={"tausworthe","tausworthe64","lfsr"}

def now(): return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
def is_linux(): return platform.system() == "Linux"
def destructive_enabled(): return os.getenv("ENABLE_DESTRUCTIVE_FIO") == "1"
def parse_extra_options(raw):
    if not raw: return {}
    if not isinstance(raw,str): raise ValueError("自定义参数必须为文本")
    parsed={}
    for line in raw.splitlines():
        line=line.strip()
        if not line: continue
        if "=" not in line: raise ValueError("自定义参数必须使用 参数=值 格式")
        key,value=(part.strip() for part in line.split("=",1))
        if key in EXTRA_OPTION_RULES:
            try: number=int(value)
            except ValueError: raise ValueError(f"自定义参数 {key} 必须为整数")
            low,high=EXTRA_OPTION_RULES[key]
            if not low <= number <= high: raise ValueError(f"自定义参数 {key} 必须在 {low} 到 {high} 之间")
            parsed[key]=number
        elif key=="random_generator":
            if value not in RANDOM_GENERATORS: raise ValueError("random_generator 仅支持 tausworthe、tausworthe64 或 lfsr")
            parsed[key]=value
        else: raise ValueError(f"不支持的自定义 fio 参数：{key}")
    return parsed
def resolve_test_config(plan, overrides):
    """合并预设与用户参数，并限制 fio 任务在可控范围内。"""
    overrides=overrides or {}
    try:
        config={"duration":int(overrides.get("duration",plan["duration"])),"block_size":str(overrides.get("block_size",plan["block_size"])),"read_ratio":int(overrides.get("read_ratio",plan["read_ratio"])),"queue_depth":int(overrides.get("queue_depth",plan["queue_depth"])),"threshold_temp":int(overrides.get("threshold_temp",plan["threshold_temp"])),"io_pattern":str(overrides.get("io_pattern","randrw")),"num_jobs":int(overrides.get("num_jobs",1)),"ramp_time":int(overrides.get("ramp_time",0)),"rate_limit":int(overrides.get("rate_limit",0)),"verify":str(overrides.get("verify","none")),"extra_options":parse_extra_options(overrides.get("extra_options",""))}
    except (TypeError, ValueError): raise ValueError("测试参数格式不正确")
    if not 1 <= config["duration"] <= 720: raise ValueError("测试时长必须在 1 到 720 小时之间")
    if config["block_size"] not in BLOCK_SIZES: raise ValueError("不支持的块大小")
    if not 0 <= config["read_ratio"] <= 100: raise ValueError("读比例必须在 0% 到 100% 之间")
    if not 1 <= config["queue_depth"] <= 1024: raise ValueError("队列深度必须在 1 到 1024 之间")
    if not 35 <= config["threshold_temp"] <= 90: raise ValueError("温度阈值必须在 35°C 到 90°C 之间")
    if config["io_pattern"] not in IO_PATTERNS: raise ValueError("不支持的 I/O 模式")
    if not 1 <= config["num_jobs"] <= 32: raise ValueError("并发作业数必须在 1 到 32 之间")
    if not 0 <= config["ramp_time"] <= 3600: raise ValueError("预热时间必须在 0 到 3600 秒之间")
    if not 0 <= config["rate_limit"] <= 20000: raise ValueError("限速必须在 0 到 20000 MB/s 之间")
    if config["verify"] not in VERIFY_MODES: raise ValueError("不支持的数据校验模式")
    return config
def command(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired): return None
def nvme_controller(path):
    """将 namespace 路径 /dev/nvmeXnY 转换为控制器路径 /dev/nvmeX。"""
    match=re.fullmatch(r"(/dev/nvme\d+)(?:n\d+)?", path)
    return match.group(1) if match else None
def collect_nvme_logs(device):
    controller=nvme_controller(device["path"])
    if not controller: raise ValueError("仅支持 NVMe 设备日志采集")
    if not shutil.which("nvme"): raise ValueError("未安装 nvme-cli，无法采集日志")
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    folder=LOG_ROOT / f"{device['id']}_{stamp}"; folder.mkdir(parents=True,exist_ok=True)
    jobs=[("全量 telemetry",["nvme","telemetry-log",controller,"-o",str(folder/"telemetry_full.log")]),("关键 telemetry",["nvme","telemetry-log",controller,"-c","-o",str(folder/"telemetry_critical.log")]),("扩展 SMART 0xC0",["nvme","get-log",controller,"-i","0xC0","-l","1024"]),("扩展 SMART 0xCA",["nvme","get-log",controller,"-i","0xCA","-l","348"])]
    results=[]
    for name,cmd in jobs:
        output_file=folder/"smart_c0.log" if "0xC0" in name else folder/"smart_ca.log" if "0xCA" in name else None
        try: result=subprocess.run(cmd,capture_output=True,text=True,timeout=180,check=False)
        except (OSError,subprocess.TimeoutExpired) as exc: results.append({"name":name,"ok":False,"message":str(exc)}); continue
        if output_file: output_file.write_text(result.stdout+("\n"+result.stderr if result.stderr else ""),encoding="utf-8")
        target=output_file or Path(cmd[-1])
        results.append({"name":name,"ok":result.returncode==0,"file":str(target.relative_to(LOG_ROOT)) if target.exists() else None,"message":(result.stderr or result.stdout)[-300:]})
    return {"controller":controller,"folder":str(folder.relative_to(LOG_ROOT)),"results":results}
def flatten(items):
    for item in items:
        yield item
        yield from flatten(item.get("children", []))

def celsius(value):
    """将 smart-log 返回的温度统一为摄氏度。

    NVMe SMART 的温度字段规范为 Kelvin；部分 nvme-cli JSON 会直接返回
    数值（例如 306），此前被界面错误标作 306°C。
    """
    if value in (None, "", "--"): return "--"
    match=re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match: return "--"
    temperature=float(match.group())
    if 200 <= temperature <= 450: temperature-=273.15
    return round(temperature, 1)

def smart(path, transport):
    """读取可选的 NVMe / SMART 遥测；工具缺失或无权限时保留 --。"""
    info={"health":"--","temperature":"--"}
    result = command(["nvme","smart-log",path,"-o","json"]) if transport == "nvme" and shutil.which("nvme") else None
    if result and result.returncode == 0:
        try:
            data=json.loads(result.stdout); info["temperature"]=celsius(data.get("temperature", "--"))
            info["health"]=data.get("percentage_used", 0); info["health"]=max(0,100-int(info["health"]))
            return info
        except (ValueError, TypeError): pass
    result = command(["smartctl","-A","-j",path]) if shutil.which("smartctl") else None
    if result and result.stdout:
        try:
            data=json.loads(result.stdout); info["temperature"]=celsius(data.get("temperature",{}).get("current","--"))
            info["health"]=100 if data.get("smart_status",{}).get("passed") else "--"
        except (ValueError, TypeError): pass
    return info

def testability(disk):
    """返回裸盘压力测试的准入结果；检查整块盘及其全部子分区。"""
    descendants=list(flatten([disk]))
    partitions=[item.get("name") for item in descendants[1:] if item.get("type")=="part"]
    mountpoints=[]
    for item in descendants:
        mountpoints.extend(m for m in (item.get("mountpoints") or []) if m)
    mountpoints=sorted(set(mountpoints))
    reasons=[]
    if str(disk.get("rota")) not in ("0", "False", "false", "None"):
        reasons.append("检测为机械旋转盘，非 SSD")
    if str(disk.get("ro")) in ("1", "True", "true"):
        reasons.append("设备为只读状态")
    if partitions:
        reasons.append("磁盘含有分区，禁止裸盘测试")
    if mountpoints:
        if any(m in ("/", "/boot", "/boot/efi", "[SWAP]") for m in mountpoints):
            reasons.append("包含系统盘、启动盘或交换分区")
        else:
            reasons.append("存在已挂载分区")
    return {"testable":not reasons,"reasons":reasons,"mountpoints":mountpoints}

def discover_linux_devices():
    if not is_linux() or not shutil.which("lsblk"): return []
    result=command(["lsblk","--json","--bytes","--output","NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,RO,MOUNTPOINTS"])
    if not result or result.returncode: return []
    try: blocks=json.loads(result.stdout).get("blockdevices",[])
    except ValueError: return []
    devices=[]
    for disk in flatten(blocks):
        if disk.get("type") != "disk": continue
        path=disk.get("path", ""); trans=(disk.get("tran") or "nvme").lower()
        if not path.startswith("/dev/"): continue
        safety=testability(disk)
        health=smart(path, trans)
        status="可测试" if safety["testable"] else "不可测试"
        devices.append({"id":disk["name"],"path":path,"name":(disk.get("model") or "Enterprise SSD").strip(),"serial":(disk.get("serial") or "未读取").strip(),"interface":"NVMe" if trans=="nvme" else trans.upper(),"capacity":f"{int(disk.get('size',0))/1024**3:.0f} GB","health":health["health"],"temperature":health["temperature"],"mounted":bool(safety["mountpoints"]),"mountpoints":safety["mountpoints"],"testable":safety["testable"],"test_reasons":safety["reasons"],"status":status})
    return devices

def demo_devices():
    return [{"id":"demo-nvme0","path":"/dev/nvme0n1","name":"Samsung PM9A3 3.84TB","serial":"DEMO-24001","interface":"NVMe Gen4","capacity":"3.49 TB","health":98,"temperature":38,"mounted":False,"mountpoints":[],"testable":True,"test_reasons":[],"status":"演示设备"}]
def load_state():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except ValueError: pass
    return {"plans":DEFAULT_PLANS,"tasks":[]}
STATE=load_state(); STATE.setdefault("plans",DEFAULT_PLANS); STATE.setdefault("tasks",[])
def persist(): DATA_FILE.write_text(json.dumps({"plans":STATE["plans"],"tasks":STATE["tasks"]},ensure_ascii=False,indent=2),encoding="utf-8")
def event(task,severity,text): task["events"].append({"time":now(),"severity":severity,"text":text}); task["events"]=task["events"][-50:]
def recover_interrupted_tasks():
    """服务重启后没有存活的线程或 fio 子进程，不能继续声明任务在运行。"""
    changed=False
    for task in STATE["tasks"]:
        if task.get("status") in ("运行中", "停止中", "排队中"):
            previous=task["status"]
            task["status"]="已中断"; task["ended_at"]=now()
            task["result"]="服务重启中断" if previous != "排队中" else "服务重启取消"
            task.setdefault("events", [])
            event(task,"警告",f"服务重启后未检测到可恢复的执行器，原状态“{previous}”任务已中断")
            changed=True
    if changed: persist()

# 不自动恢复真实 fio 任务，避免服务重启后发生未经确认的裸盘写入。
recover_interrupted_tasks()
def sample_health(task):
    info=smart(task["path"],task["transport"].lower()); return {"time":now(),"temperature":info["temperature"],"p99":"--","throughput":"--","health":info["health"]}
def launch_task(task):
    """锁内调用：同一块盘仅由此函数启动一个后台执行器。"""
    task["status"]="运行中"; task["started_at"]=now()
    event(task,"信息","排队任务已开始执行" if task.get("queued") else "任务已开始执行")
    task["queued"]=False
    runner=fio_runner if task["mode"]=="真实 fio 裸盘" else demo_runner
    threading.Thread(target=runner,args=(task["id"],),daemon=True).start()

def launch_next():
    """锁内调用：全局只运行一个任务，按创建先后启动下一条。"""
    if any(t["status"] in ("运行中","停止中") for t in STATE["tasks"]): return
    next_task=next((t for t in reversed(STATE["tasks"]) if t["status"]=="排队中"),None)
    if next_task: launch_task(next_task)

def finish(task,result):
    task["status"]="已完成"; task["ended_at"]=now(); task["result"]=result; event(task,"信息",f"测试完成，稳定性结论：{result}"); launch_next(); persist()

def demo_runner(task_id):
    random.seed(task_id)
    while True:
        time.sleep(2)
        with LOCK:
            task=next((x for x in STATE["tasks"] if x["id"]==task_id),None)
            if not task or task["status"]!="运行中": return
            task["elapsed"]+=10; task["progress"]=min(100,round(task["elapsed"]/(task["duration"]*60)*100,1)); p=task["progress"]
            s={"time":now(),"temperature":round(38+18*p/100+random.uniform(-2,2),1),"p99":round(3+10*p/100+random.random()*2,2),"throughput":round(2400-420*p/100+random.uniform(-80,80)),"health":round(100-p*.015,2)}; task["samples"].append(s); task["samples"]=task["samples"][-200:]
            if s["temperature"]>=task["threshold_temp"] and not task.get("temp_alerted"): event(task,"严重",f"温度 {s['temperature']}°C 达到阈值 {task['threshold_temp']}°C"); task["temp_alerted"]=True
            if s["p99"]>14 and not task.get("latency_alerted"): event(task,"警告",f"P99 延迟升至 {s['p99']} ms"); task["latency_alerted"]=True
            if p>=100: finish(task,"预警" if task.get("temp_alerted") or task.get("latency_alerted") else "通过"); return
            persist()

def fio_runner(task_id):
    with LOCK:
        task=next(x for x in STATE["tasks"] if x["id"]==task_id)
        if task["status"]!="运行中": return
        runtime=task["duration"]*3600
        cmd=["fio",f"--name=enterprise-ssd-{task_id}",f"--filename={task['path']}","--direct=1","--ioengine=libaio","--time_based=1",f"--runtime={runtime}",f"--rw={task['io_pattern']}",f"--bs={task['block_size']}",f"--iodepth={task['queue_depth']}",f"--numjobs={task['num_jobs']}",f"--ramp_time={task['ramp_time']}","--group_reporting=1","--output-format=json"]
        if task["io_pattern"]=="randrw": cmd.append(f"--rwmixread={task['read_ratio']}")
        if task["rate_limit"]: cmd.append(f"--rate={task['rate_limit']}M")
        if task["verify"]!="none": cmd.append(f"--verify={task['verify']}")
        cmd.extend(f"--{key}={value}" for key,value in task.get("extra_options",{}).items())
        event(task,"信息",f"已启动 fio 裸盘测试：{task['path']}（该设备上的数据将被覆盖）"); persist()
    try: process=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True); PROCESSES[task_id]=process
    except OSError as exc:
        with LOCK: task["status"]="失败"; task["result"]="执行器错误"; task["ended_at"]=now(); event(task,"严重",str(exc)); launch_next(); persist()
        return
    started=time.time()
    while process.poll() is None:
        time.sleep(15)
        with LOCK:
            if task["status"]!="运行中": process.terminate(); break
            task["elapsed"]=int(time.time()-started); task["progress"]=min(99.9,round(task["elapsed"]/runtime*100,1)); s=sample_health(task); task["samples"].append(s); task["samples"]=task["samples"][-200:]
            try:
                if float(s["temperature"])>=task["threshold_temp"] and not task.get("temp_alerted"): event(task,"严重",f"温度 {s['temperature']}°C 达到阈值"); task["temp_alerted"]=True
            except (ValueError,TypeError): pass
            persist()
    stdout,stderr=process.communicate(); PROCESSES.pop(task_id,None)
    with LOCK:
        if task["status"]=="停止中":
            task["status"]="已停止"; task["ended_at"]=now(); task["result"]="人工终止"; event(task,"警告","fio 进程已停止"); launch_next(); persist(); return
        if task["status"]=="已停止": return
        if process.returncode:
            task["status"]="失败"; task["result"]="fio 执行失败"; task["ended_at"]=now(); event(task,"严重",(stderr or "fio 返回异常")[-300:]); launch_next(); persist(); return
        try:
            job=json.loads(stdout)["jobs"][0]; p99=max(job["read"].get("clat_ns",{}).get("percentile",{}).get("99.000000",0),job["write"].get("clat_ns",{}).get("percentile",{}).get("99.000000",0))/1e6; bw=(job["read"].get("bw_bytes",0)+job["write"].get("bw_bytes",0))/1e6
            task["samples"].append({"time":now(),"temperature":"--","p99":round(p99,2),"throughput":round(bw,1),"health":"--"})
        except (ValueError,KeyError,IndexError,TypeError): event(task,"警告","fio 已完成，但无法解析部分性能摘要")
        task["progress"]=100; finish(task,"预警" if task.get("temp_alerted") else "通过")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/"static"),**kwargs)
    def log_message(self,*args): pass
    def send_json(self,body,status=200):
        raw=json.dumps(body,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
    def state(self):
        devices=discover_linux_devices() if is_linux() else demo_devices()
        return {"devices":devices,"plans":STATE["plans"],"tasks":STATE["tasks"],"environment":{"linux":is_linux(),"fio":bool(shutil.which("fio")),"destructive_enabled":destructive_enabled(),"root":getattr(os,"geteuid",lambda:1)()==0}}
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state":
            with LOCK: return self.send_json(self.state())
        if path.startswith("/api/logs/"):
            try:
                file=(LOG_ROOT/unquote(path.removeprefix("/api/logs/"))).resolve()
                if LOG_ROOT.resolve() not in file.parents or not file.is_file(): raise FileNotFoundError
                raw=file.read_bytes(); self.send_response(200); self.send_header("Content-Type","application/octet-stream"); self.send_header("Content-Disposition",f'attachment; filename="{file.name}"'); self.send_header("Content-Length",str(len(raw))); self.end_headers(); return self.wfile.write(raw)
            except FileNotFoundError: return self.send_json({"error":"日志文件不存在"},404)
        if path.startswith("/api/log-archives/"):
            try:
                folder=(LOG_ROOT/unquote(path.removeprefix("/api/log-archives/"))).resolve()
                if LOG_ROOT.resolve() not in folder.parents or not folder.is_dir(): raise FileNotFoundError
                buffer=io.BytesIO()
                with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
                    for file in folder.iterdir():
                        if file.is_file(): archive.write(file,file.name)
                raw=buffer.getvalue(); self.send_response(200); self.send_header("Content-Type","application/zip"); self.send_header("Content-Disposition",f'attachment; filename="{folder.name}.zip"'); self.send_header("Content-Length",str(len(raw))); self.end_headers(); return self.wfile.write(raw)
            except FileNotFoundError: return self.send_json({"error":"日志目录不存在"},404)
        if path.startswith("/api/report/"):
            with LOCK:
                task=next((x for x in STATE["tasks"] if x["id"]==path.rsplit("/",1)[-1]),None)
                if not task:return self.send_json({"error":"任务不存在"},404)
                rows="".join(f"<li><b>{e['time']} [{e['severity']}]</b> {e['text']}</li>" for e in task["events"]); last=task["samples"][-1] if task["samples"] else {}
            page=f"<!doctype html><meta charset=utf-8><title>SSD 测试报告</title><style>body{{font:15px Arial;margin:48px;color:#172033}}h1{{color:#1d4ed8}}.c{{border:1px solid #dbe2ef;padding:18px;border-radius:10px;margin:16px 0}}</style><h1>企业级 SSD 稳定性与耐久测试报告</h1><p>生成：{now()} | 模式：{task['mode']}</p><div class=c>设备：{task['device']}（{task['serial']}）<br>路径：{task['path']}<br>策略：{task['plan']} / {task['duration']} 小时<br>结论：{task['status']} / {task['result']}<br>最后遥测：温度 {last.get('temperature','--')}°C，P99 {last.get('p99','--')}ms，吞吐 {last.get('throughput','--')}MB/s</div><div class=c><h2>事件</h2><ul>{rows}</ul></div>".encode()
            self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Disposition",'attachment; filename="enterprise-ssd-report.html"');self.send_header("Content-Length",str(len(page)));self.end_headers();return self.wfile.write(page)
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            data=self.body()
            with LOCK:
                if path=="/api/tasks":
                    devices=discover_linux_devices() if is_linux() else demo_devices(); device=next((x for x in devices if x["id"]==data.get("device_id")),None); plan=next((x for x in STATE["plans"] if x["id"]==data.get("plan_id")),None); mode=data.get("mode","demo")
                    if not device or not plan:return self.send_json({"error":"请选择有效设备和策略"},400)
                    if not device.get("testable", False):
                        return self.send_json({"error":"该 SSD 不满足测试准入条件："+"；".join(device.get("test_reasons",["设备状态未知"]))},403)
                    if mode=="real":
                        if not (is_linux() and shutil.which("fio") and destructive_enabled() and getattr(os,"geteuid",lambda:1)()==0): return self.send_json({"error":"真实压测要求 Linux、root、fio 与 ENABLE_DESTRUCTIVE_FIO=1"},403)
                        if not data.get("confirmed_device"): return self.send_json({"error":"请确认当前选择的是专用测试 SSD，且允许覆盖其数据"},403)
                    config=resolve_test_config(plan,data.get("config"))
                    customized=any(config[key]!=plan[key] for key in ("duration","block_size","read_ratio","queue_depth","threshold_temp")) or any(config[key]!=default for key,default in {"io_pattern":"randrw","num_jobs":1,"ramp_time":0,"rate_limit":0,"verify":"none","extra_options":{}}.items())
                    busy=any(t["status"] in ("运行中","停止中") for t in STATE["tasks"])
                    task={"id":uuid.uuid4().hex[:8],"name":f"{device['name']} · {plan['name']}","device":device["name"],"serial":device["serial"],"path":device["path"],"transport":device["interface"],"plan":plan["name"]+("（自定义参数）" if customized else ""),"duration":config["duration"],"block_size":config["block_size"],"read_ratio":config["read_ratio"],"queue_depth":config["queue_depth"],"threshold_temp":config["threshold_temp"],"io_pattern":config["io_pattern"],"num_jobs":config["num_jobs"],"ramp_time":config["ramp_time"],"rate_limit":config["rate_limit"],"verify":config["verify"],"extra_options":config["extra_options"],"mode":"真实 fio 裸盘" if mode=="real" else "安全演示","status":"排队中" if busy else "运行中","result":"--","started_at":None,"ended_at":None,"elapsed":0,"progress":0,"samples":[],"events":[],"queued":busy}
                    event(task,"信息","已有测试任务正在运行，任务已进入全局队列" if busy else "任务已创建");STATE["tasks"].insert(0,task)
                    if not busy: launch_task(task)
                    persist();return self.send_json(task,201)
                if path.startswith("/api/devices/") and path.endswith("/logs"):
                    device_id=path.split("/")[3]; devices=discover_linux_devices() if is_linux() else demo_devices(); device=next((x for x in devices if x["id"]==device_id),None)
                    if not device:return self.send_json({"error":"设备不存在"},404)
                    return self.send_json(collect_nvme_logs(device),201)
                if path.startswith("/api/tasks/") and path.endswith("/stop"):
                    task=next((x for x in STATE["tasks"] if x["id"]==path.split("/")[3]),None)
                    if not task:return self.send_json({"error":"任务不存在"},404)
                    if task["status"]=="排队中":
                        task["status"]="已停止";task["ended_at"]=now();task["result"]="已取消";event(task,"信息","排队任务已取消");persist()
                    elif task["status"]=="运行中":
                        process=PROCESSES.get(task["id"]);
                        if task["mode"]=="真实 fio 裸盘" and process:
                            task["status"]="停止中";event(task,"警告","正在终止 fio 进程，设备释放后将继续下一条任务");process.terminate();persist()
                        else:
                            task["status"]="已停止";task["ended_at"]=now();task["result"]="人工终止";event(task,"警告","任务由操作员停止");launch_next();persist()
                    return self.send_json(task)
            return self.send_json({"error":"接口不存在"},404)
        except (json.JSONDecodeError,KeyError,ValueError,OSError) as exc:return self.send_json({"error":f"请求处理失败：{exc}"},400)
if __name__=="__main__":
    print("SSD PressureTest: http://127.0.0.1:8080"); ThreadingHTTPServer(("127.0.0.1",int(os.getenv("PORT","8080"))),Handler).serve_forever()

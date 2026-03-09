from dataclasses import dataclass
from math import ceil

TITLE_STEPS={0:(0,"без звания"),10:(1000,"Ученик"),20:(2000,"Подмастерье"),30:(4000,"Ремесленник"),40:(8000,"Мастеровой"),50:(16000,"Искусник"),60:(30000,"Старший ремесленник"),70:(60000,"Цеховой"),80:(120000,"Наставник"),90:(250000,"Зодчий"),100:(500000,"Архитектор")}

@dataclass
class LevelState:
    level:int
    title:str
    current_level_xp_floor:int
    next_level_xp_target:int


def build_level_state(xp:int)->LevelState:
    anchors=sorted(TITLE_STEPS.items(), key=lambda x:x[0])
    if xp<=0:
        return LevelState(0,TITLE_STEPS[0][1],0,100)
    full=[]
    for i in range(len(anchors)-1):
        sl,(sx,st)=anchors[i]
        el,(ex,_)=anchors[i+1]
        step=(ex-sx)/(el-sl)
        for lvl in range(sl,el):
            full.append((lvl,int(round(sx+step*(lvl-sl))),st))
    full.append((100,TITLE_STEPS[100][0],TITLE_STEPS[100][1]))
    cur=full[0]
    nxt=(1,100,"без звания")
    for idx,item in enumerate(full):
        if xp>=item[1]:
            cur=item
            nxt=full[idx+1] if idx+1<len(full) else item
        else:
            break
    return LevelState(cur[0],cur[2],cur[1],nxt[1])


def calculate_master_grant_cost_tenths(resource:str, amount:int)->int:
    if amount<=0:
        raise ValueError("amount must be positive")
    resource=resource.lower()
    if resource=='lux':
        return max(1, ceil(amount/10))
    if resource=='xp':
        return max(1, ceil(amount/100))
    raise ValueError("resource must be xp or lux")


def calculate_sealconvert_lux(seals_tenths:int, fee_percent:int=8):
    if seals_tenths<=0:
        raise ValueError("seals_tenths must be positive")
    gross=seals_tenths*10
    fee=max(1, int(round(gross*(fee_percent/100))))
    net=max(0, gross-fee)
    return gross, fee, net

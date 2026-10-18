# -*- coding: utf-8 -*-
"""
从游戏 data.unity3d 提取官方卡片数据：
  CARD_Prop  -> 游戏实装卡的内部ID序列（贴图名数字）
  CARD_Indx  -> 卡名偏移表
  CARD_Name  -> 各语言卡名（简中/英/日...）
三者顺序一致，第 i 个 ID 对应第 i 个名字。
解密算法来自社区（akintos / RndUser0），XOR + zlib，密钥可暴力搜索。
"""
import zlib
import struct
import json
from pathlib import Path

import UnityPy

# 第一张卡（ID 3001, Unknown 占位）在各语言里的译名，用作语言判定锚点
LANG_ANCHORS = {
    "ja": "アンノウン",
    "en": "Unknown",
    "es": "Desconocido",
    "pt": "Desconhecido",
    "fr": "Inconnu",
    "de": "Unbekannt",
    "it": "Sconosciuto",
    "ko": "언논",
}
# 简繁锚点卡 4007（青眼白龙）
CN_SIMPLIFIED_4007 = "青眼白龙"


def _xor(data: bytes, key: int) -> bytearray:
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ ((((i + key + 0x23D) * key) ^ (i % 7)) & 0xFF)
    return out


def _find_key(sample: bytes, max_key: int = 1_000_000) -> int:
    """用 zlib 头首字节(0x78)快速过滤，再尝试解压验证"""
    target0 = sample[0] ^ 0x78
    decobj = zlib.decompressobj()
    for key in range(max_key):
        if (((key + 0x23D) * key) & 0xFF) != target0:
            continue
        head = bytes(_xor(sample[:64], key))
        try:
            decobj = zlib.decompressobj()
            decobj.decompress(head)
            # 头部通过，再完整解压确认
            zlib.decompress(_xor(sample, key))
            return key
        except zlib.error:
            continue
    raise RuntimeError("未找到 CARD 文件加密密钥")


def _text_assets(env):
    props, names, indxs = [], {}, {}
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        d = o.read()
        b = d.m_Script
        if isinstance(b, str):
            b = b.encode("utf-8", "surrogateescape")
        b = bytes(b)
        if d.m_Name == "CARD_Prop":
            props.append(b)
        elif d.m_Name == "CARD_Name":
            names[o.path_id] = b
        elif d.m_Name == "CARD_Indx":
            indxs[o.path_id] = b
    return props, names, indxs


def _prop_ids(prop_dec: bytes):
    """从偏移8起，每8字节一条，低2字节 LE uint16 为内部ID"""
    return [struct.unpack_from("<H", prop_dec, p)[0]
            for p in range(8, len(prop_dec) - 1, 8)]


def _name_offsets(indx_dec: bytes):
    """每8字节一条，低4字节 LE uint32 为Name偏移；丢弃首条"""
    vals = [struct.unpack_from("<I", indx_dec, p)[0]
            for p in range(0, len(indx_dec) - 3, 8)]
    return vals[1:]


def _split(text: bytes, offs):
    res = []
    for i in range(len(offs) - 1):
        seg = text[offs[i]:offs[i + 1]].rstrip(b"\x00")
        res.append(seg.decode("utf-8"))
    return res


def extract_game_cards(game_root) -> dict:
    """
    返回 {"cards": {str(id): {"cn","en","ja"}}, "count": n, "key": hex}
    只包含游戏内实装（CARD_Prop 中存在）的卡。
    """
    data_path = Path(game_root) / "masterduel_Data" / "data.unity3d"
    env = UnityPy.load(str(data_path))
    props, names_enc, indxs_enc = _text_assets(env)
    if not props:
        raise RuntimeError("data.unity3d 中未找到 CARD_Prop")

    key = _find_key(props[0])
    prop_dec = zlib.decompress(_xor(props[0], key))
    ids = _prop_ids(prop_dec)

    # 部分语言副本可能解密/解压失败，逐个容错跳过
    tables = {}
    for pid, b in indxs_enc.items():
        try:
            tables[pid] = _name_offsets(zlib.decompress(_xor(b, key)))
        except zlib.error:
            continue

    # 解出每个语言版本的名字列表
    lang_lists = {}  # lang -> [name per id]
    for npid, nb in names_enc.items():
        try:
            text = zlib.decompress(_xor(nb, key))
        except zlib.error:
            continue
        for ipid, offs in tables.items():
            if len(offs) - 1 != len(ids):
                continue
            if not (8 <= offs[0] and offs[-1] <= len(text)):
                continue
            if any(offs[i] > offs[i + 1] for i in range(len(offs) - 1)):
                continue
            try:
                nl = _split(text, offs)
            except UnicodeDecodeError:
                continue
            # 语言判定：第一张卡(id 3001)的锚点译名
            first = nl[0]
            lang = None
            for L, anchor in LANG_ANCHORS.items():
                if first == anchor:
                    lang = L
                    break
            if first == "未知":
                lang = "zh-CN" if (ids.index(4007) < len(nl)
                                   and nl[ids.index(4007)] == CN_SIMPLIFIED_4007) else "zh-TW"
            if lang:
                lang_lists[lang] = nl
            break

    if "zh-CN" not in lang_lists:
        raise RuntimeError("未能识别游戏简体中文卡名表")

    cn = lang_lists["zh-CN"]
    en = lang_lists.get("en", [""] * len(ids))
    ja = lang_lists.get("ja", [""] * len(ids))

    cards = {}
    for i, cid in enumerate(ids):
        cards[str(cid)] = {
            "cn": cn[i] if i < len(cn) else "",
            "en": en[i] if i < len(en) else "",
            "ja": ja[i] if i < len(ja) else "",
        }
    return {"cards": cards, "ids": [str(x) for x in ids],
            "count": len(cards), "key": hex(key),
            "languages": sorted(lang_lists.keys())}


def save_game_names(game_root, out_path: Path) -> dict:
    """提取官方卡名并写入 JSON：{"__ids__": [有序卡号], 卡号: {cn,en,ja}}。
    __ids__ 按 CARD_Prop 顺序（约等于游戏收录顺序），游戏更新后重跑即可加入新卡。"""
    res = extract_game_cards(game_root)
    data = {"__ids__": res["ids"]}
    data.update(res["cards"])
    out_path = Path(out_path)
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return res


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\Administrator\Steam\steamapps\common\Yu-Gi-Oh!  Master Duel"
    r = extract_game_cards(root)
    print("卡片数:", r["count"], "密钥:", r["key"], "语言:", r["languages"])
    for cid in ("3001", "3167", "4007", "4041"):
        print(cid, r["cards"].get(cid))

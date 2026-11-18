"""图像帧模式（image mode）Prompt 渲染测试（todo 22）。

video 模式必须与改动前模板逐字节一致（下方快照取自改动前模板的实际渲染；原文含超过
100 字符的行，而仓库 ruff line-length 为 100，故以 base64 字面量保存，解码后逐字节
还原）；image 模式新增帧时间戳列表、短事件规则 5/6 与 8 事件软上限。
"""

import base64
from datetime import datetime

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.services.prompt_builder.v2.video_recognition import build_video_recognition_prompt
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES

# 改动前模板渲染出的 video 模式 system prompt 全文（todo 22 快照，模板改动前
# 用原模板渲染并记录），新的 video 模式渲染必须逐字节相等。
VIDEO_MODE_SNAPSHOT = base64.b64decode(
    "5L2g5piv5a625bqt55uR5o6n6KeG6aKR6K+G5Yir5Yqp5omL77yM55uu5qCH5piv5o+Q5Y+W57uT5p6E5YyW5LqL"
    "5Lu25bm26L6T5Ye65Y+v6Kej5p6QIEpTT07jgIIKCuinhOWIme+8mgoxLiDkuovlrp7kvJjlhYjvvJrlhYjmj4/o"
    "v7Dlj6/op4Llr5/ooYzkuLrvvIzlho3lgZrovbvluqbmpoLmi6zjgIIKMi4g5L+d5a6I6K+G5Yir77ya6Lqr5Lu9"
    "5LiN56Gu5a6a5pe25L2/55SoIHN1c3BlY3RlZCDmiJYgdW5rbm93bu+8jOemgeatouiZmuaehOWQjeWtl+OAggoz"
    "LiDlkb3lkI3kvJjlhYjvvJrlvZPor4bliKvnirbmgIHkuLogY29uZmlybWVkIOS4lOWPr+WMuemFjeWutuW6reah"
    "o+ahiOaXtu+8jHN1bW1hcnlfdGV4dCDkuI7kuovku7bmlofmnKzkvJjlhYjkvb/nlKjmoaPmoYjlp5PlkI3vvIjm"
    "iJDlkZjkuI7lrqDnianpg73pgILnlKjvvInjgIIKNC4g54q25oCB6L6555WM77ya56aB5q2i6L6T5Ye65byA5b+D"
    "L+eUn+eXhS/mipHpg4HnrYnkuLvop4LmiJbljLvlrablv4PnkIbliKTmlq3jgIIKNS4g5LqL5Lu257KS5bqm77ya"
    "6L+e57ut5ZCM57G75rS75Yqo5bC96YeP5ZCI5bm277yM5LuF5Zyo5piO5pi+5Y+Y5YyW5pe25ouG5YiG44CCCjYu"
    "IOaXtumXtOinhOWIme+8mm9mZnNldF9zdGFydF9zZWMgLyBvZmZzZXRfZW5kX3NlYyDku6XlvZPliY3ovpPlhaXo"
    "p4bpopHniYfmrrXotbfngrnkuLogMCDnp5LvvIzkuI3mmK8gc2Vzc2lvbiDotbfngrnjgIIKNy4g6aOO5qC86L65"
    "55WM77ya6aOO5qC85Y+q5b2x5ZON6Ieq54S26K+t6KiA5a2X5q615o6q6L6e77yM5LiN5pS55Y+Y6K+G5Yir6L65"
    "55WM44CCCjguIOi+k+WHuue6puadn++8muS7hei+k+WHuiBKU09OIOWvueixoe+8jOS4jeimgei+k+WHuumineWk"
    "luaWh+Wtl+OAggo5LiDlubLmibDmjpLpmaTvvJrms6jmhI/nlLXop4blj43lhYnjgIHnjrvnkoPlj43lsITjgIHo"
    "tbDlu4rov5zlpITlsI/nm67moIfnrYnor6/liKTpo47pmanjgIIKMTAuIOivreiogOe7n+S4gO+8muaJgOacieiH"
    "queEtuivreiogOWtl+aute+8iHRpdGxl44CBc3VtbWFyeeOAgWRldGFpbOOAgXN1bW1hcnlfdGV4dOOAgW1haW5f"
    "c3ViamVjdHPjgIFvYnNlcnZlZF9hY3Rpb25z44CBaW50ZXJwcmV0ZWRfc3RhdGXjgIFkaXNwbGF5X25hbWXjgIFh"
    "bmFseXNpc19ub3Rlcy5ub3Rl77yJ5LiA5b6L5L2/55So566A5L2T5Lit5paH77yb56aB5q2i6Iux5paH5Y2V6K+N"
    "44CB6Iux5paH5p6a5Li+5ZCN5LiO5ou86Z+z44CCCjExLiDlj5blgLzop4TojIPvvJpvYnNlcnZlZF9hY3Rpb25z"
    "IOWPquWhq+WPr+inguWvn+eahOS4reaWh+WKqOS9nOefreivje+8iOWmguKAnOermeeri+KAneKAnOi1sOWKqOKA"
    "neKAnOWXheaOouKAne+8ie+8jGludGVycHJldGVkX3N0YXRlIOWPquWhq+WPr+inguWvn+eahOS4reaWh+eKtuaA"
    "geefreivje+8iOWmguKAnOmdmeatouKAneKAnOitpuinieKAneKAnOWcqOWcuuKAne+8ie+8jOS6jOiAheS4jeW+"
    "l+i+k+WHuuiLseaWhyB0b2tlbuOAggoxMi4g5ZCN56ew6KeE6IyD77yaZGlzcGxheV9uYW1lIOW/hemhu+aYr+WP"
    "r+ivu+S4reaWh+ensOiwk++8iOaIkOWRmOeUqOaho+ahiOWQjeaIluKAnOeIuOeIuC/lpojlpojigJ3nrYnop5Lo"
    "ibLlkI3vvIzlrqDniannlKjmoaPmoYjlkI3miJbigJznjKsv54uX4oCd562J77yM6ZmM55Sf5Lq655So4oCc6ZmM"
    "55Sf5Lq64oCd4oCc5pyq6K+G5Yir5Lq65ZGY4oCd77yJ77yM56aB5q2i6L6T5Ye6IHVua25vd25fcGVyc29u44CB"
    "dW5rbm93bl9wZXTjgIF1bmtub3du44CBc3VzcGVjdGVkX2NhdCDnrYnoi7HmlofljaDkvY3nrKbvvIzkuZ/kuI3o"
    "poHmt7vliqDigJznlpHkvLzigJ3liY3nvIDjgIIKMTMuIOaho+ahiOWMuemFje+8mm1hdGNoZWRfcHJvZmlsZV9u"
    "YW1lIOS7heW9k+WvueixoeS4juWutuW6reaho+ahiOS4reafkOS4quWnk+WQjeS4gOiHtOaXtuWhq+WGme+8jOS4"
    "lOW/hemhu+S4juaho+ahiOS4reaWh+Wnk+WQjemAkOWtl+ebuOWQjO+8m+emgeatouaLvOmfs+OAgeiLseaWh+e/"
    "u+ivkeaIluWIq+WQje+8iOWmgiBGZWlmZWnjgIFNb23jgIFMaXpp77yJ44CC5peg5rOV6YCQ5a2X5Yy56YWN5pe2"
    "5aGrIG51bGzjgIIKMTQuIOWFs+azqOmhueaJk+agh++8muiLpeS6i+S7tuWRveS4reWutuW6reWFs+azqOmhue+8"
    "jOWwhuWFtiBrZXkg5aGr5YWlIGZvY3VzX21hdGNoZXPvvJvlj6rog73kvb/nlKjlrrbluq3kuIrkuIvmlofliJfl"
    "h7rnmoQga2V577yM5peg5Yy56YWN5pe25aGrIFtd77yM56aB5q2i6Ieq6YCgIGtleeOAggoK6L6T5Ye6IHNjaGVt"
    "Ye+8iOekuuS+i+S7heekuuiMg+Wtl+auteagvOW8j+S4juS4reaWh+WGmeazle+8jOWunumZheWGheWuueaMiein"
    "humikeWhq+WGme+8m+Wtl+autee7k+aehOW/hemhu+S4peagvOmBteWuiO+8iToKewogICJzZXNzaW9uX3N1bW1h"
    "cnkiOiB7CiAgICAic3VtbWFyeV90ZXh0IjogIuWuouWOheWGheS4gOWQjeaIkOWRmOWcqOWOqOaIv+WMuuWfn+WB"
    "nOeVmeaVtOeQhueJqeWTge+8jOmaj+WQjuemu+W8gOOAgiIsCiAgICAiYWN0aXZpdHlfbGV2ZWwiOiAibG93IiwK"
    "ICAgICJtYWluX3N1YmplY3RzIjogWyLlpojlpogiXQogIH0sCiAgImV2ZW50cyI6IFsKICAgIHsKICAgICAgIm9m"
    "ZnNldF9zdGFydF9zZWMiOiAwLAogICAgICAib2Zmc2V0X2VuZF9zZWMiOiAxMiwKICAgICAgImV2ZW50X3R5cGUi"
    "OiAibWVtYmVyX3N0YXkiLAogICAgICAidGl0bGUiOiAi5aaI5aaI5Zyo5Y6o5oi/5Yy65Z+f5YGc55WZIiwKICAg"
    "ICAgInN1bW1hcnkiOiAi5aaI5aaI5Zyo55S76Z2i5Y+z5L6n5Y6o5oi/5Yy65Z+f5YGc55WZ77yM5pW055CG54mp"
    "5ZOB44CCIiwKICAgICAgImRldGFpbCI6ICLnlLvpnaLlj7PkvqfvvIzkuIDlkI3lpbPmgKfvvIjlpojlpojvvInn"
    "q5nlnKjlhrDnrrHlkozmqbHmn5zliY3vvIzlpKfpg6jliIbml7bpl7Tkv53mjIHnq5nnq4vvvIzlgbblsJTmnInm"
    "iYvpg6jliqjkvZzjgIIiLAogICAgICAicmVsYXRlZF9lbnRpdGllcyI6IFsKICAgICAgICB7CiAgICAgICAgICAi"
    "ZW50aXR5X3R5cGUiOiAibWVtYmVyIiwKICAgICAgICAgICJkaXNwbGF5X25hbWUiOiAi5aaI5aaIIiwKICAgICAg"
    "ICAgICJtYXRjaGVkX3Byb2ZpbGVfbmFtZSI6ICLlpojlpogiLAogICAgICAgICAgInJlY29nbml0aW9uX3N0YXR1"
    "cyI6ICJjb25maXJtZWQiLAogICAgICAgICAgImNvbmZpZGVuY2UiOiAwLjkKICAgICAgICB9CiAgICAgIF0sCiAg"
    "ICAgICJvYnNlcnZlZF9hY3Rpb25zIjogWyLnq5nnq4siLCAi5pW055CG54mp5ZOBIl0sCiAgICAgICJpbnRlcnBy"
    "ZXRlZF9zdGF0ZSI6IFsi6Z2Z5q2iIl0sCiAgICAgICJmb2N1c19tYXRjaGVzIjogWyJtZW1iZXJfaW5vdXQiXSwK"
    "ICAgICAgImNvbmZpZGVuY2UiOiAwLjkKICAgIH0KICBdLAogICJhbmFseXNpc19ub3RlcyI6IFt7InR5cGUiOiAi"
    "aWRlbnRpdHlfdW5jZXJ0YWluIiwgIm5vdGUiOiAi6Led56a76L6D6L+c77yM5peg5rOV56Gu6K6k6Lqr5Lu944CC"
    "In1dCn0="
).decode("utf-8")

# 改动前模板渲染出的 video 模式 user prompt 全文（同样固定输入），逐字节相等。
USER_MODE_SNAPSHOT = base64.b64decode(
    "5a625bqt5LiK5LiL5paH77yaCuWutuW6reamguWGtTog5a625bqt5ZCNPeaIkeeahOWutuW6rTsg5qCH562+PWhh"
    "c19wZXQ7IOWFs+azqOmhuTog5pegOyDpo47moLw9ZmFtaWx5X2NvbXBhbmlvbjsg6aOO5qC85YGP5aW9PeeugOa0"
    "gTsg5Yqp5omL5ZCNPeWutuW6reWKqeaJizsg5aSH5rOoPeWknOmXtOi+g+WuiemdmS4K5a625bqt5oiQ5ZGYOiDm"
    "l6AuCuWuoOeJqeS/oeaBrzog5pegLgrovrnnlYw6IOWutuW6reaho+ahiOS7heS9nOS4uui+heWKqeS4iuS4i+aW"
    "h++8jOS4jeiDveabv+S7o+inhumikeivgeaNri4KCuaRhOWDj+WktOS4iuS4i+aWh++8mgrmkYTlg4/lpLQ957Gz"
    "5a62TkFTOyDlkI3np7A95a6i5Y6FOyDkvY3nva495a6i5Y6FOyDlpIfms6g955S16KeG5bGP5bmV5Lya5Y+N5YWJ"
    "LgoK5Lu75Yqh57qnIFByb21wdO+8mgrku7vliqHnm67moIc6IOWvueW9k+WJjSBzZXNzaW9uIOWBmue7k+aehOWM"
    "luivhuWIq++8jOi+k+WHuuWPr+WFpeW6k+S6i+S7tuOAggrkvJror53kuIrkuIvmloc6IHNlc3Npb25faWQ9MTAw"
    "OyBzb3VyY2VfaWQ9MTsgc3RhcnQ9MjAyNi0wMy0xM1QxMDowMDowMDsgZW5kPTIwMjYtMDMtMTNUMTA6MDE6MDA7"
    "IGR1cmF0aW9uPTYwOyBzZWdtZW50X2luZGV4PTA7IHNlZ21lbnRfc3RhcnRfb2Zmc2V0X3NlYz0wOyBzZWdtZW50"
    "X2R1cmF0aW9uX3NlY29uZHM9NjAuCuetlueVpeS4iuS4i+aWhzogaW5nZXN0X3R5cGU9eGlhb21pX25hc19iYWNr"
    "dXA7IHNvdXJjZV90eXBlPWxvY2FsX2RpcmVjdG9yeTsgc3RyYXRlZ3lfbm90ZT3kvJjlhYjlvZLnurMuCuS6i+S7"
    "tuaKveWPluinhOWImToKZXZlbnRfdHlwZSDlrprkuYnvvIjlv4Xpobvku47kuK3pgInmi6nvvIznpoHmraLoh6rp"
    "gKDmiJbovpPlh7roi7HmlofliKvlkI3vvIk6Ci0gbWVtYmVyX2FwcGVhcjog5a625bqt5oiQ5ZGY5Ye6546w5Zyo"
    "55S76Z2i5Lit77yI6aaW5qyh5Ye6546w5oiW55+t5pqC57uP6L+H77yM5peg5piO56Gu6L+b5Ye65Yqo5L2c77yJ"
    "Ci0gbWVtYmVyX2VudGVyOiDmiJDlkZjov5vlhaXnlLvpnaLmiJbmn5DkuKrljLrln5/vvIjmnInmmI7noa7nmoTo"
    "v5vlhaXliqjkvZzkuI7mlrnlkJHvvIkKLSBtZW1iZXJfbGVhdmU6IOaIkOWRmOemu+W8gOeUu+mdouaIluafkOS4"
    "quWMuuWfn++8iOacieaYjuehrueahOemu+W8gOWKqOS9nOS4juaWueWQke+8iQotIG1lbWJlcl9zdGF5OiDmiJDl"
    "kZjlnKjnlLvpnaLlhoXmjIHnu63lgZznlZnvvIjlnKjlnLrml7bpl7TovoPplb/kuJTml6DmmI7noa7ov5vlh7rl"
    "iqjkvZzvvIkKLSB1bmtub3duX3BlcnNvbl9hcHBlYXI6IOmZjOeUn+S6ui/mnKrnn6XkurrlkZjlh7rnjrDvvIjm"
    "l6Dms5XljLnphY3lrrbluq3moaPmoYjnmoTkurrlkZjvvIkKLSBwZXRfYXBwZWFyOiDlrqDnianlh7rnjrDlnKjn"
    "lLvpnaLkuK3vvIjpppbmrKHlh7rnjrDmiJbnn63mmoLnu4/ov4fvvIkKLSBwZXRfZW50ZXI6IOWuoOeJqei/m+WF"
    "peeUu+mdouaIluafkOS4quWMuuWfn++8iOacieaYjuehrueahOi/m+WFpeWKqOS9nO+8iQotIHBldF9sZWF2ZTog"
    "5a6g54mp56a75byA55S76Z2i5oiW5p+Q5Liq5Yy65Z+f77yI5pyJ5piO56Gu55qE56a75byA5Yqo5L2c77yJCi0g"
    "cGV0X3N0YXk6IOWuoOeJqeWcqOeUu+mdouWGheaMgee7reWBnOeVme+8iOaXoOaYjuaYvui/m+WHuuWKqOS9nOOA"
    "geS5n+WIpOWumuS4jeWHuua0u+WKqOaIluS8keaBr++8iQotIHBldF9hY3Rpdml0eTog5a6g54mp5rS75Yqo77yI"
    "6LWw5Yqo44CB5aWU6LeR44CB546p6ICN44CB5ZeF5o6i562J5piO56Gu5rS75Yqo77yJCi0gcGV0X3Jlc3Q6IOWu"
    "oOeJqeS8keaBr+aIlumdmeatou+8iOi2tOWNp+OAgeedoeinieOAgemVv+aXtumXtOS4jeWKqO+8iQotIG1lbWJl"
    "cl9wZXRfaW50ZXJhY3Rpb246IOaIkOWRmOS4juWuoOeJqeS6kuWKqAotIG11bHRpX2VudGl0eV9pbnRlcmFjdGlv"
    "bjog5aSa5Liq5a+56LGh5LmL6Ze05LqS5YqoCi0gYWJub3JtYWxfc3RheTog5byC5bi45YGc55WZ77yI6ZW/5pe2"
    "6Ze05LiN5Yqo44CB5byC5bi45L2N572u562J77yJCi0gc2NlbmVfYXR0ZW50aW9uX25lZWRlZDog5Zy65pmv6ZyA"
    "6KaB5YWz5rOo77yI5byC5bi45YWJ57q/44CB54mp5ZOB56e75Yqo562J77yJCi0gb3RoZXI6IOaXoOazleW9kuWF"
    "peS7peS4iuS7u+S9leWFt+S9k+exu+Wei+aXtuaJjeS9v+eUqO+8jOemgeatoueUqOWug+WFnOW6leW4uOingeWc"
    "uuaZrwrnsbvlnovovrnnlYzvvIjpgb/lhY3mt7fnlKjvvIk6Ci0gYXBwZWFyL2VudGVyL2xlYXZlIOS+p+mHjeKA"
    "nOWHuueOsC/ov5vlhaUv56a75byA55qE5Yqo5L2c5LiO5pa55ZCR4oCd77ybc3RheSDkvqfph43igJzlnKjlnLrl"
    "gZznlZnigJ3jgIIKLSBwZXRfYWN0aXZpdHkg5oyH5pyJ5piO56Gu5rS75Yqo77yI6LWw5YqoL+WllOi3kS/njqno"
    "gI0v5ZeF5o6i77yJ77ybcGV0X3Jlc3Qg5oyH5LyR5oGv5oiW6Z2Z5q2i77yI6La05Y2nL+edoeinie+8ie+8m+S7"
    "heWcqOWcuuS9huaXoOaYjuaYvua0u+WKqOaXtueUqCBwZXRfc3RheeOAggotIG1lbWJlcl9hcHBlYXIg55So5LqO"
    "6aaW5qyh5Ye6546w5oiW55+t5pqC57uP6L+H77yb5oyB57ut5Zyo5Zy65LiU5peg6L+b5Ye65Yqo5L2c55SoIG1l"
    "bWJlcl9zdGF544CCCi0g5LuF5b2T5peg5rOV5b2S5YWl5Lu75L2V5YW35L2T57G75Z6L5pe25omN55SoIG90aGVy"
    "77yM56aB5q2i55SoIG90aGVyIOWFnOW6leW4uOingeWcuuaZr+OAggphbmFseXNpc19ub3RlX3R5cGUg5p6a5Li+"
    "PWlkZW50aXR5X3VuY2VydGFpbuOAgWxvd192aXNpYmlsaXR544CBbm9fc2lnbmlmaWNhbnRfZXZlbnTjgIFvY2Ns"
    "dXNpb27jgIFwb3NzaWJsZV9mYWxzZV9wb3NpdGl2ZeOAgXNjZW5lX25vaXNlOyBtZXJnZV9zaW1pbGFyX2FjdGlv"
    "bnM9dHJ1ZTsgc3BsaXRfb25seV9vbl9zaWduaWZpY2FudF9jaGFuZ2U9dHJ1ZS4K5pyA57uI57qm5p2fOiDlj6ro"
    "g73ovpPlh7ogSlNPTiDlr7nosaE7IOaJgOaciSBvZmZzZXQg5b+F6aG75Li66Z2e6LSf5pWw5LiUIGVuZD49c3Rh"
    "cnQ7IG9mZnNldCDku6XlvZPliY3ovpPlhaXop4bpopHniYfmrrXotbfngrnkuLogMCDnp5I7IGV2ZW50X3R5cGUg"
    "5b+F6aG75p2l6Ieq5LiK6Z2i5a6a5LmJOyBkZXRhaWwg6ZyA6K+m57uG5o+P6L+w5Y+v6KeC5a+f5LqL5a6eOyDm"
    "iYDmnInoh6rnhLbor63oqIDlrZfmrrXkvb/nlKjnroDkvZPkuK3mlocu"
).decode("utf-8")

ORIGINAL_RULE_5 = "5. 事件粒度：连续同类活动尽量合并，仅在明显变化时拆分。"
ORIGINAL_RULE_6 = (
    "6. 时间规则：offset_start_sec / offset_end_sec "
    "以当前输入视频片段起点为 0 秒，不是 session 起点。"
)
IMAGE_RULE_5 = "5. 事件粒度：优先输出多个短事件，只有同一持续行为才合并。"
IMAGE_RULE_6 = (
    "6. 时间规则：offset_start_sec / offset_end_sec "
    "以本批第 0 帧为 0 秒，且必须对齐到给定帧时间戳。"
)
EVENT_CAP_SENTENCE = "每个文件最多输出 8 个事件"
FRAME_LIST_HEADER = "本批帧时间戳："


def _base_input() -> dict:
    """与 tests/unit/test_video_recognition_prompt_builder.py 相同的固定输入。"""
    return {
        "home_context": {
            "home_profile": {
                "home_name": "我的家庭",
                "family_tags": ["has_pet"],
                "focus_points": ["pet_status"],
                "system_style": "family_companion",
                "style_preference_text": "简洁",
                "assistant_name": "家庭助手",
                "home_note": "夜间较安静",
            },
            "members": [],
            "pets": [],
        },
        "video_source": {
            "source_name": "米家NAS",
            "camera_name": "客厅",
            "location_name": "客厅",
            "prompt_text": "电视屏幕会反光",
            "source_type": "local_directory",
        },
        "session_context": {
            "session_id": 100,
            "source_id": 1,
            "session_start_time": datetime(2026, 3, 13, 10, 0, 0),
            "session_end_time": datetime(2026, 3, 13, 10, 1, 0),
            "total_duration_seconds": 60,
            "segment_index": 0,
            "segment_start_offset_sec": 0,
            "segment_duration_seconds": 60,
        },
        "strategy_context": {
            "ingest_type": "xiaomi_nas_backup",
            "source_type": "local_directory",
            "strategy_note": "优先归纳",
        },
    }


def _frame_lines(user_prompt: str) -> list[str]:
    return [line for line in user_prompt.splitlines() if line.startswith("帧 ")]


def test_video_mode_system_prompt_matches_pre_change_snapshot() -> None:
    system_prompt, _ = build_video_recognition_prompt(_base_input())

    assert system_prompt == VIDEO_MODE_SNAPSHOT


def test_video_mode_user_prompt_matches_pre_change_snapshot() -> None:
    _, user_prompt = build_video_recognition_prompt(_base_input())

    assert user_prompt == USER_MODE_SNAPSHOT


def test_video_mode_keeps_original_rule5_rule6_and_has_no_frame_header() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(_base_input())

    assert ORIGINAL_RULE_5 in system_prompt
    assert ORIGINAL_RULE_6 in system_prompt
    assert FRAME_LIST_HEADER not in user_prompt


def test_image_mode_renders_three_frame_lines_in_order() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(
        {
            **_base_input(),
            "input_mode": "image",
            "frame_timestamps": [0.0, 3.5, 7.0],
        }
    )

    expected_section = f"{FRAME_LIST_HEADER}\n帧 1/3 — t=0.0s\n帧 2/3 — t=3.5s\n帧 3/3 — t=7.0s"
    assert expected_section in user_prompt
    assert user_prompt.count(FRAME_LIST_HEADER) == 1
    assert _frame_lines(user_prompt) == ["帧 1/3 — t=0.0s", "帧 2/3 — t=3.5s", "帧 3/3 — t=7.0s"]

    # image 模式下规则 5/6 换成图像版，且包含 8 事件软上限；video 版规则不再出现。
    assert IMAGE_RULE_5 in system_prompt
    assert IMAGE_RULE_6 in system_prompt
    assert ORIGINAL_RULE_5 not in system_prompt
    assert ORIGINAL_RULE_6 not in system_prompt
    assert EVENT_CAP_SENTENCE in system_prompt

    # 16 个事件类型名必须全部保留。
    for event_type in sorted(VIDEO_EVENT_TYPES):
        assert event_type in user_prompt


def test_image_mode_single_frame() -> None:
    _, user_prompt = build_video_recognition_prompt(
        {
            **_base_input(),
            "input_mode": "image",
            "frame_timestamps": [12.5],
        }
    )

    assert f"{FRAME_LIST_HEADER}\n帧 1/1 — t=12.5s" in user_prompt
    assert _frame_lines(user_prompt) == ["帧 1/1 — t=12.5s"]


def test_image_mode_empty_timestamps_renders_header_only() -> None:
    # 空帧列表允许渲染（只有表头、无帧行）；“image 模式空 frame_timestamps 属于
    # 调用方错误”的校验留给 todo 23，本测试只锁定当前渲染行为。
    _, user_prompt = build_video_recognition_prompt(
        {
            **_base_input(),
            "input_mode": "image",
            "frame_timestamps": [],
        }
    )

    assert user_prompt.count(FRAME_LIST_HEADER) == 1
    assert _frame_lines(user_prompt) == []


def _compiler_input(**overrides: object) -> VideoRecognitionPromptInput:
    base = {
        "home_context": _base_input()["home_context"],
        "video_source": VideoSourcePromptContext(
            source_name="米家NAS",
            camera_name="客厅",
            location_name="客厅",
            prompt_text="电视屏幕会反光",
            source_type="local_directory",
        ),
        "session_context": SessionPromptContext(
            session_id=100,
            source_id=1,
            session_start_time=datetime(2026, 3, 13, 10, 0, 0),
            session_end_time=datetime(2026, 3, 13, 10, 1, 0),
            total_duration_seconds=60,
        ),
        "strategy_context": StrategyPromptContext(
            ingest_type="xiaomi_nas_backup",
            source_type="local_directory",
            strategy_note="优先归纳",
        ),
        "event_type_list": sorted(VIDEO_EVENT_TYPES),
    }
    base.update(overrides)
    return VideoRecognitionPromptInput(**base)


def test_compiler_defaults_preserve_video_mode() -> None:
    system_prompt, user_prompt = compile_video_recognition_prompt(_compiler_input())

    assert system_prompt == VIDEO_MODE_SNAPSHOT
    assert FRAME_LIST_HEADER not in user_prompt


def test_compiler_image_mode_passes_frame_list() -> None:
    system_prompt, user_prompt = compile_video_recognition_prompt(
        _compiler_input(input_mode="image", frame_timestamps=[0.0, 3.5, 7.0])
    )

    assert IMAGE_RULE_5 in system_prompt
    assert EVENT_CAP_SENTENCE in system_prompt
    assert _frame_lines(user_prompt) == ["帧 1/3 — t=0.0s", "帧 2/3 — t=3.5s", "帧 3/3 — t=7.0s"]

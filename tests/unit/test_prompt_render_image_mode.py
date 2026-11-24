"""图像帧模式（image mode）Prompt 渲染测试（todo 22）。

video 模式快照用于锁定识别 prompt 的渲染结果，模板每次有意修订都要同步更新快照；
image 模式新增帧时间戳列表、短事件规则 5/6 与 17 事件软上限。
"""

import base64
import json
from datetime import datetime

import pytest

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.services.prompt_builder.v2.video_recognition import build_video_recognition_prompt
from src.services.video_analysis.enums import INTERPRETED_STATES, VIDEO_EVENT_TYPES

# 当前模板渲染出的 video 模式 system prompt 全文（识别 prompt 修订后重新生成；原文含
# 超过 100 字符的行，而仓库 ruff line-length 为 100，故以 base64 字面量保存，解码后逐字节
# 还原）。
VIDEO_MODE_SNAPSHOT = base64.b64decode(
    "5L2g5piv5a625bqt55uR5o6n6KeG6aKR6K+G5Yir5Yqp5omL77yM55uu5qCH5piv5o+Q5Y+W57uT"
    "5p6E5YyW5LqL5Lu25bm26L6T5Ye65Y+v6Kej5p6QIEpTT07jgIIKCuinhOWIme+8mgoxLiDkuovl"
    "rp7kvJjlhYjvvJrlhYjmj4/ov7Dlj6/op4Llr5/ooYzkuLrvvIzlho3lgZrovbvluqbmpoLmi6zj"
    "gIIKMi4g5L+d5a6I6K+G5Yir77ya6Lqr5Lu95LiN56Gu5a6a5pe25L2/55SoIHN1c3BlY3RlZCDm"
    "iJYgdW5rbm93bu+8jOemgeatouiZmuaehOWQjeWtl+OAggozLiDlkb3lkI3kvJjlhYjvvJrlvZPo"
    "r4bliKvnirbmgIHkuLogY29uZmlybWVkIOS4lOWPr+WMuemFjeWutuW6reaho+ahiOaXtu+8jHN1"
    "bW1hcnlfdGV4dCDkuI7kuovku7bmlofmnKzkvJjlhYjkvb/nlKjmoaPmoYjlp5PlkI3vvIjmiJDl"
    "kZjkuI7lrqDnianpg73pgILnlKjvvInjgIIKNC4g54q25oCB6L6555WM77ya56aB5q2i6L6T5Ye6"
    "5byA5b+DL+eUn+eXhS/mipHpg4HnrYnkuLvop4LmiJbljLvlrablv4PnkIbliKTmlq3jgIIKNS4g"
    "5LqL5Lu257KS5bqm77ya6L+e57ut5ZCM57G75rS75Yqo5bC96YeP5ZCI5bm277yM5LuF5Zyo5piO"
    "5pi+5Y+Y5YyW5pe25ouG5YiG44CCCjYuIOaXtumXtOinhOWIme+8mm9mZnNldF9zdGFydF9zZWMg"
    "LyBvZmZzZXRfZW5kX3NlYyDku6XlvZPliY3ovpPlhaXop4bpopHniYfmrrXotbfngrnkuLogMCDn"
    "p5LvvIzkuI3mmK8gc2Vzc2lvbiDotbfngrnjgIIKNy4g6aOO5qC86L6555WM77ya6aOO5qC85Y+q"
    "5b2x5ZON6Ieq54S26K+t6KiA5a2X5q615o6q6L6e77yM5LiN5pS55Y+Y6K+G5Yir6L6555WM44CC"
    "CjguIOi+k+WHuue6puadn++8muS7hei+k+WHuiBKU09OIOWvueixoe+8jOS4jeimgei+k+WHuumi"
    "neWkluaWh+Wtl+OAggo5LiDlubLmibDmjpLpmaTvvJrms6jmhI/nlLXop4blj43lhYnjgIHnjrvn"
    "koPlj43lsITjgIHotbDlu4rov5zlpITlsI/nm67moIfnrYnor6/liKTpo47pmanjgIIKMTAuIOiv"
    "reiogOe7n+S4gO+8muaJgOacieiHqueEtuivreiogOWtl+aute+8iHRpdGxl44CBc3VtbWFyeeOA"
    "gWRldGFpbOOAgXN1bW1hcnlfdGV4dOOAgW1haW5fc3ViamVjdHPjgIFvYnNlcnZlZF9hY3Rpb25z"
    "44CBaW50ZXJwcmV0ZWRfc3RhdGXjgIFkaXNwbGF5X25hbWXjgIFhbmFseXNpc19ub3Rlcy5ub3Rl"
    "77yJ5LiA5b6L5L2/55So566A5L2T5Lit5paH77yb56aB5q2i6Iux5paH5Y2V6K+N44CB6Iux5paH"
    "5p6a5Li+5ZCN5LiO5ou86Z+z44CCCjExLiDlj5blgLzop4TojIPvvJpvYnNlcnZlZF9hY3Rpb25z"
    "IOWPquWhq+WPr+inguWvn+eahOS4reaWh+WKqOS9nOefreivje+8iOWmguKAnOermeeri+KAneKA"
    "nOi1sOWKqOKAneKAnOWXheaOouKAne+8ie+8m2ludGVycHJldGVkX3N0YXRlIOW/hemhu+S7juS7"
    "u+WKoee6pyBQcm9tcHQg5YiX5Ye655qE5Y+X5o6n6K+N6KGo5Lit6YCJ5Y+W77yI5aaC4oCc5Zyo"
    "5Zy64oCd4oCc5LyR5oGv4oCd4oCc6Z2Z5q2i4oCd77yJ77yM56aB5q2i6Ieq6YCg6L+R5LmJ6K+N"
    "77yI5aaC4oCc56e75Yqo5Lit4oCd4oCc56a75Zy64oCd4oCc5rS76LeD4oCd77yJ5LiO6Iux5paH"
    "IHRva2Vu44CCCjEyLiDlkI3np7Dop4TojIPvvJpkaXNwbGF5X25hbWUg5b+F6aG75piv5Y+v6K+7"
    "5Lit5paH56ew6LCT77yI5oiQ5ZGY55So5qGj5qGI5ZCN5oiW4oCc54i454i4L+WmiOWmiOKAneet"
    "ieinkuiJsuWQje+8jOWuoOeJqeeUqOaho+ahiOWQjeaIluKAnOeMqy/ni5figJ3nrYnvvIzpmYzn"
    "lJ/kurrnlKjigJzpmYznlJ/kurrigJ3igJzmnKror4bliKvkurrlkZjigJ3vvInvvIznpoHmraLo"
    "vpPlh7ogdW5rbm93bl9wZXJzb27jgIF1bmtub3duX3BldOOAgXVua25vd27jgIFzdXNwZWN0ZWRf"
    "Y2F0IOetieiLseaWh+WNoOS9jeespu+8jOS5n+S4jeimgea3u+WKoOKAnOeWkeS8vOKAneWJjee8"
    "gOOAggoxMy4g5qGj5qGI5Yy56YWN77yabWF0Y2hlZF9wcm9maWxlX25hbWUg5LuF5b2T5a+56LGh"
    "5LiO5a625bqt5qGj5qGI5Lit5p+Q5Liq5aeT5ZCN5LiA6Ie05pe25aGr5YaZ77yM5LiU5b+F6aG7"
    "5LiO5qGj5qGI5Lit5paH5aeT5ZCN6YCQ5a2X55u45ZCM77yb56aB5q2i5ou86Z+z44CB6Iux5paH"
    "57+76K+R5oiW5Yir5ZCN77yI5aaCIEZlaWZlaeOAgU1vbeOAgUxpemnvvInjgILml6Dms5XpgJDl"
    "rZfljLnphY3ml7bloasgbnVsbOOAggoxNC4g5YWz5rOo6aG55omT5qCH77ya6Iul5LqL5Lu25ZG9"
    "5Lit5a625bqt5YWz5rOo6aG577yM5bCG5YW2IGtleSDloavlhaUgZm9jdXNfbWF0Y2hlc++8m+WP"
    "quiDveS9v+eUqOWutuW6reS4iuS4i+aWh+WIl+WHuueahCBrZXnvvIzml6DljLnphY3ml7bloasg"
    "W13vvIznpoHmraLoh6rpgKAga2V544CCCjE1LiDlj5jljJbliKTlrprvvJrmr5TovoPnm7jpgrvl"
    "uKcv6L+e57ut55S76Z2i5Lit5Li75L2T55qE5L2N572u44CB5ae/5oCB44CB5pWw6YeP5LiO6L+b"
    "5Ye677yb5Y+q5pyJ5Ye6546w5Y+v6KeC5a+f5Y+Y5YyW5pe25omN55Sf5oiQ5LqL5Lu244CC6Iul"
    "5Li75L2T5Zyo5pys5q615YaF6aaW5qyh5Ye6546wL+emu+W8gO+8jOaIluWnv+aAgeWPkeeUn+aY"
    "juaYvuWPmOWMlu+8iOWmguermeeri+KGkui2tOWNp++8ie+8jOS7jeW6lOiusOW9leWvueW6lOS6"
    "i+S7tu+8m+aVtOauteaXoOS7u+S9leWPr+inguWvn+WPmOWMluaXtu+8jGV2ZW50cyDlv4Xpobvk"
    "uLrnqbrmlbDnu4TvvIzlubblnKggYW5hbHlzaXNfbm90ZXMg5Lit5LulIG5vX3NpZ25pZmljYW50"
    "X2V2ZW50IOivtOaYju+8jOemgeatouS4uuS6huWhq+a7oeekuuS+i+e7k+aehOiAjOe8lumAoOS6"
    "i+S7tuOAguKAnOmdmeatouKAneKAnOS8keaBr+KAneacrOi6q+S4jeaYr+S6i+S7tu+8m+WPquac"
    "ieWnv+aAgeWPkeeUn+WPr+inguWvn+WPmOWMlu+8iOWmguermeeri+KGkui2tOWNp++8ieaIluS4"
    "u+S9k+i/m+WHuuaXtuaJjeeUn+aIkOS6i+S7tuOAggoxNi4g5pGY6KaB6KeE6IyD77yac3VtbWFy"
    "eV90ZXh0IOS4juavj+S4quS6i+S7tueahCBzdW1tYXJ5IOWPquWuouingumZiOi/sOWPr+inguWv"
    "n+S6i+Wunu+8iOS4u+S9k+eUqOW3suehruiupOeahOaho+ahiOWQje+8jOingeinhOWImSAxOO+8"
    "m+WcqOWTqu+8m+WBmuS6huS7gOS5iOaIluWkhOS6juS7gOS5iOeKtuaAge+8ie+8jOS4jeimgeWG"
    "meawm+WbtOOAgeaDhee7quaIluWPmeS6i+aAp+aPj+WGme+8m+aXoOWunui0qOWPmOWMluaXtuWG"
    "meKAnOeUu+mdouaXoOaYjuaYvuWPmOWMluKAneOAggoxOC4g56ew6LCT5LiA6Ie077ya5b2T5p+Q"
    "5Li75L2T55qEIG1hdGNoZWRfcHJvZmlsZV9uYW1lIOmdnuepuuaXtu+8jHRpdGxl44CBc3VtbWFy"
    "eeOAgWRldGFpbOOAgXN1bW1hcnlfdGV4dOOAgW1haW5fc3ViamVjdHMg5LiA5b6L5Lul6K+l5qGj"
    "5qGI5ZCN5L2c5Li76K+t77yM56aB5q2i5pS555So4oCc5LiA5Y+qL+i/meWPqiArIOeJqeenjeKA"
    "neWGmeazle+8m2RldGFpbCDpppbmrKHmj5Dlj4rlj6/lhpnmiJDigJzmoaPmoYjlkI3vvIjlpJbo"
    "p4Lnn63or63vvInigJ3vvIzkvYYgdGl0bGUg5LiOIHN1bW1hcnkg5LiN5b6X5Y+q5Ye6546w54mp"
    "56eN56ew6LCT44CC5LuF5b2TIG1hdGNoZWRfcHJvZmlsZV9uYW1lIOS4uiBudWxsIOaXtuaJjeWF"
    "geiuuOKAnOm7keeMq+KAneKAnOa3seiJsueMq+KAneetieaPj+i/sOaAp+ensOiwk++8jOW5tuaK"
    "iuS4jeehruWumuWOn+WboOWGmei/myBhbmFseXNpc19ub3Rlcy5pZGVudGl0eV91bmNlcnRhaW7v"
    "vIzlkIzml7blnKjor6Xlrp7kvZPnmoQgY2FuZGlkYXRlX3Byb2ZpbGVfbmFtZXMg5Lit5aGr5YaZ"
    "5YCZ6YCJ5qGj5qGI5ZCN77yI5pyA5aSaIDIg5Liq77yJ44CCCgrovpPlh7ogc2NoZW1h77yI56S6"
    "5L6L5LuF56S66IyD5a2X5q615qC85byP5LiO5Lit5paH5YaZ5rOV77yM5a6e6ZmF5YaF5a655oyJ"
    "6KeG6aKR5aGr5YaZ77yb5a2X5q6157uT5p6E5b+F6aG75Lil5qC86YG15a6I77yJOgp7CiAgInNl"
    "c3Npb25fc3VtbWFyeSI6IHsKICAgICJzdW1tYXJ5X3RleHQiOiAi5a6i5Y6F5YaF5LiA5ZCN5oiQ"
    "5ZGY5Zyo5Y6o5oi/5Yy65Z+f5pW055CG54mp5ZOB77yM6ZqP5ZCO56a75byA44CCIiwKICAgICJh"
    "Y3Rpdml0eV9sZXZlbCI6ICJtZWRpdW0iLAogICAgIm1haW5fc3ViamVjdHMiOiBbIuWmiOWmiCJd"
    "CiAgfSwKICAiZXZlbnRzIjogWwogICAgewogICAgICAib2Zmc2V0X3N0YXJ0X3NlYyI6IDAsCiAg"
    "ICAgICJvZmZzZXRfZW5kX3NlYyI6IDEyLAogICAgICAiZXZlbnRfdHlwZSI6ICJtZW1iZXJfc3Rh"
    "eSIsCiAgICAgICJ0aXRsZSI6ICLlpojlpojlnKjljqjmiL/ljLrln5/mlbTnkIbnianlk4EiLAog"
    "ICAgICAic3VtbWFyeSI6ICLlpojlpojlnKjnlLvpnaLlj7PkvqfljqjmiL/ljLrln5/mlbTnkIbn"
    "ianlk4HjgIIiLAogICAgICAiZGV0YWlsIjogIueUu+mdouWPs+S+p++8jOWmiOWmiO+8iOS4gOWQ"
    "jeWls+aAp++8ieermeWcqOWGsOeuseWSjOapseafnOWJje+8jOWkp+mDqOWIhuaXtumXtOS/neaM"
    "geermeeri++8jOWBtuWwlOacieaJi+mDqOWKqOS9nOOAgiIsCiAgICAgICJyZWxhdGVkX2VudGl0"
    "aWVzIjogWwogICAgICAgIHsKICAgICAgICAgICJlbnRpdHlfdHlwZSI6ICJtZW1iZXIiLAogICAg"
    "ICAgICAgImRpc3BsYXlfbmFtZSI6ICLlpojlpogiLAogICAgICAgICAgIm1hdGNoZWRfcHJvZmls"
    "ZV9uYW1lIjogIuWmiOWmiCIsCiAgICAgICAgICAiY2FuZGlkYXRlX3Byb2ZpbGVfbmFtZXMiOiBb"
    "XSwKICAgICAgICAgICJyZWNvZ25pdGlvbl9zdGF0dXMiOiAiY29uZmlybWVkIiwKICAgICAgICAg"
    "ICJjb25maWRlbmNlIjogMC45CiAgICAgICAgfQogICAgICBdLAogICAgICAib2JzZXJ2ZWRfYWN0"
    "aW9ucyI6IFsi56uZ56uLIiwgIuaVtOeQhueJqeWTgSJdLAogICAgICAiaW50ZXJwcmV0ZWRfc3Rh"
    "dGUiOiBbIuWcqOWcuiJdLAogICAgICAiZm9jdXNfbWF0Y2hlcyI6IFsibWVtYmVyX2lub3V0Il0s"
    "CiAgICAgICJjb25maWRlbmNlIjogMC45CiAgICB9CiAgXSwKICAiYW5hbHlzaXNfbm90ZXMiOiBb"
    "eyJ0eXBlIjogInNjZW5lX25vaXNlIiwgIm5vdGUiOiAi55S16KeG5bGP5bmV5a2Y5Zyo5Y+N5YWJ"
    "44CCIn1dCn0KCuepuue7k+aenOekuuS+i++8iOaVtOauteaXoOWunui0qOWPmOWMluaXtu+8m2V2"
    "ZW50cyDlv4XpobvkuLrnqbrvvIzkuI3opoHnvJbpgKDkuovku7bvvIk6CnsKICAic2Vzc2lvbl9z"
    "dW1tYXJ5IjogewogICAgInN1bW1hcnlfdGV4dCI6ICLnlLvpnaLml6DmmI7mmL7lj5jljJbjgIIi"
    "LAogICAgImFjdGl2aXR5X2xldmVsIjogImxvdyIsCiAgICAibWFpbl9zdWJqZWN0cyI6IFtdCiAg"
    "fSwKICAiZXZlbnRzIjogW10sCiAgImFuYWx5c2lzX25vdGVzIjogW3sidHlwZSI6ICJub19zaWdu"
    "aWZpY2FudF9ldmVudCIsICJub3RlIjogIuebuOmCu+W4p+S4reS4u+S9k+eahOS9jee9ruOAgeWn"
    "v+aAgeS4juaVsOmHj+aXoOaYjuaYvuWPmOWMluOAgiJ9XQp9CgrlpJrkuLvkvZPnp7DosJPnpLrk"
    "vovvvIjkuIDlj6rog73noa7orqTmoaPmoYjlkI3jgIHkuIDlj6rkuI3og73vvJvms6jmhI8gdGl0"
    "bGUvc3VtbWFyeS9kZXRhaWwg5LiJ5aSE56ew6LCT5LiA6Ie077yJOgp7CiAgImV2ZW50cyI6IFsK"
    "ICAgIHsKICAgICAgIm9mZnNldF9zdGFydF9zZWMiOiAwLAogICAgICAib2Zmc2V0X2VuZF9zZWMi"
    "OiAyMCwKICAgICAgImV2ZW50X3R5cGUiOiAicGV0X2FjdGl2aXR5IiwKICAgICAgInRpdGxlIjog"
    "IuWYn+WYn+WcqOWxj+mjjumZhOi/kei1sOWKqOWXheaOoiIsCiAgICAgICJzdW1tYXJ5IjogIuWY"
    "n+WYn+WcqOeUu+mdouW3puS+p+Wxj+mjjuS4juWNq+eUn+mXtOmXqOWPo+S5i+mXtOadpeWbnui1"
    "sOWKqOW5tuWXheaOouWcsOmdouOAgiIsCiAgICAgICJkZXRhaWwiOiAi55S76Z2i5bem5LiL6KeS"
    "77yM5Zif5Zif77yI5LiA5Y+q546z55GB54yr77yJ5Zyo5bGP6aOO6ZmE6L+R6LWw5Yqo5bm25ZeF"
    "5o6i5Zyw6Z2i77yb5Y+z5ZCO5pa55Y+m5pyJ5LiA5Y+q5rex6Imy54yr57yT5oWi57uP6L+H44CC"
    "IiwKICAgICAgInJlbGF0ZWRfZW50aXRpZXMiOiBbCiAgICAgICAgewogICAgICAgICAgImVudGl0"
    "eV90eXBlIjogInBldCIsCiAgICAgICAgICAiZGlzcGxheV9uYW1lIjogIuWYn+WYnyIsCiAgICAg"
    "ICAgICAibWF0Y2hlZF9wcm9maWxlX25hbWUiOiAi5Zif5ZifIiwKICAgICAgICAgICJjYW5kaWRh"
    "dGVfcHJvZmlsZV9uYW1lcyI6IFtdLAogICAgICAgICAgInJlY29nbml0aW9uX3N0YXR1cyI6ICJj"
    "b25maXJtZWQiLAogICAgICAgICAgImNvbmZpZGVuY2UiOiAwLjk1CiAgICAgICAgfSwKICAgICAg"
    "ICB7CiAgICAgICAgICAiZW50aXR5X3R5cGUiOiAicGV0IiwKICAgICAgICAgICJkaXNwbGF5X25h"
    "bWUiOiAi5rex6Imy54yrIiwKICAgICAgICAgICJtYXRjaGVkX3Byb2ZpbGVfbmFtZSI6IG51bGws"
    "CiAgICAgICAgICAiY2FuZGlkYXRlX3Byb2ZpbGVfbmFtZXMiOiBbIuWiqOWiqCIsICLlmJ/lmJ8i"
    "XSwKICAgICAgICAgICJyZWNvZ25pdGlvbl9zdGF0dXMiOiAic3VzcGVjdGVkIiwKICAgICAgICAg"
    "ICJjb25maWRlbmNlIjogMC41CiAgICAgICAgfQogICAgICBdLAogICAgICAib2JzZXJ2ZWRfYWN0"
    "aW9ucyI6IFsi6LWw5YqoIiwgIuWXheaOoiJdLAogICAgICAiaW50ZXJwcmV0ZWRfc3RhdGUiOiBb"
    "Iua0u+WKqCIsICLnp7vliqgiXSwKICAgICAgImZvY3VzX21hdGNoZXMiOiBbInBldF9zdGF0dXMi"
    "XSwKICAgICAgImNvbmZpZGVuY2UiOiAwLjkKICAgIH0KICBdLAogICJhbmFseXNpc19ub3RlcyI6"
    "IFsKICAgIHsKICAgICAgInR5cGUiOiAiaWRlbnRpdHlfdW5jZXJ0YWluIiwKICAgICAgImVudGl0"
    "eV9pbmRleCI6IDEsCiAgICAgICJjYW5kaWRhdGVzIjogWyLloqjloqgiLCAi5Zif5ZifIl0sCiAg"
    "ICAgICJub3RlIjogIuWPs+WQjuaWuea3seiJsueMq+WboOWFiee6v+S4jui3neemu+aXoOazleeh"
    "ruiupOaYr+WiqOWiqOi/mOaYr+WYn+WYn+OAgiIKICAgIH0KICBdCn0KCuS7u+WKoee6pyBQcm9t"
    "cHTvvJoK5Lu75Yqh55uu5qCHOiDlr7nlvZPliY0gc2Vzc2lvbiDlgZrnu5PmnoTljJbor4bliKvv"
    "vIzovpPlh7rlj6/lhaXlupPkuovku7bjgIIK5LqL5Lu25oq95Y+W6KeE5YiZOgpldmVudF90eXBl"
    "IOWumuS5ie+8iOW/hemhu+S7juS4remAieaLqe+8jOemgeatouiHqumAoOaIlui+k+WHuuiLseaW"
    "h+WIq+WQje+8iToKLSBtZW1iZXJfYXBwZWFyOiDlrrbluq3miJDlkZjlh7rnjrDlnKjnlLvpnaLk"
    "uK3vvIjpppbmrKHlh7rnjrDmiJbnn63mmoLnu4/ov4fvvIzml6DmmI7noa7ov5vlh7rliqjkvZzv"
    "vIkKLSBtZW1iZXJfZW50ZXI6IOaIkOWRmOi/m+WFpeeUu+mdouaIluafkOS4quWMuuWfn++8iOac"
    "ieaYjuehrueahOi/m+WFpeWKqOS9nOS4juaWueWQke+8iQotIG1lbWJlcl9sZWF2ZTog5oiQ5ZGY"
    "56a75byA55S76Z2i5oiW5p+Q5Liq5Yy65Z+f77yI5pyJ5piO56Gu55qE56a75byA5Yqo5L2c5LiO"
    "5pa55ZCR77yJCi0gbWVtYmVyX3N0YXk6IOaIkOWRmOWcqOeUu+mdouWGheaMgee7reWBnOeVme+8"
    "iOWcqOWcuuaXtumXtOi+g+mVv+S4lOaXoOaYjuehrui/m+WHuuWKqOS9nO+8iQotIHVua25vd25f"
    "cGVyc29uX2FwcGVhcjog6ZmM55Sf5Lq6L+acquefpeS6uuWRmOWHuueOsO+8iOaXoOazleWMuemF"
    "jeWutuW6reaho+ahiOeahOS6uuWRmO+8iQotIHBldF9hcHBlYXI6IOWuoOeJqeWHuueOsOWcqOeU"
    "u+mdouS4re+8iOmmluasoeWHuueOsOaIluefreaague7j+i/h++8iQotIHBldF9lbnRlcjog5a6g"
    "54mp6L+b5YWl55S76Z2i5oiW5p+Q5Liq5Yy65Z+f77yI5pyJ5piO56Gu55qE6L+b5YWl5Yqo5L2c"
    "77yJCi0gcGV0X2xlYXZlOiDlrqDniannprvlvIDnlLvpnaLmiJbmn5DkuKrljLrln5/vvIjmnInm"
    "mI7noa7nmoTnprvlvIDliqjkvZzvvIkKLSBwZXRfc3RheTog5a6g54mp5Zyo55S76Z2i5YaF5oyB"
    "57ut5YGc55WZ77yI5peg5piO5pi+6L+b5Ye65Yqo5L2c44CB5Lmf5Yik5a6a5LiN5Ye65rS75Yqo"
    "5oiW5LyR5oGv77yJCi0gcGV0X2FjdGl2aXR5OiDlrqDnianmtLvliqjvvIjotbDliqjjgIHlpZTo"
    "t5HjgIHnjqnogI3jgIHll4XmjqLnrYnmmI7noa7mtLvliqjvvIkKLSBwZXRfcmVzdDog5a6g54mp"
    "5LyR5oGv5oiW6Z2Z5q2i77yI6La05Y2n44CB552h6KeJ44CB6ZW/5pe26Ze05LiN5Yqo77yJCi0g"
    "bWVtYmVyX3BldF9pbnRlcmFjdGlvbjog5oiQ5ZGY5LiO5a6g54mp5LqS5YqoCi0gbXVsdGlfZW50"
    "aXR5X2ludGVyYWN0aW9uOiDlpJrkuKrlr7nosaHkuYvpl7TkupLliqgKLSBhYm5vcm1hbF9zdGF5"
    "OiDlvILluLjlgZznlZnvvIjplb/ml7bpl7TkuI3liqjjgIHlvILluLjkvY3nva7nrYnvvIkKLSBz"
    "Y2VuZV9hdHRlbnRpb25fbmVlZGVkOiDlnLrmma/pnIDopoHlhbPms6jvvIjlvILluLjlhYnnur/j"
    "gIHnianlk4Hnp7vliqjnrYnvvIkKLSBvdGhlcjog5peg5rOV5b2S5YWl5Lul5LiK5Lu75L2V5YW3"
    "5L2T57G75Z6L5pe25omN5L2/55So77yM56aB5q2i55So5a6D5YWc5bqV5bi46KeB5Zy65pmvCuex"
    "u+Wei+i+ueeVjO+8iOmBv+WFjea3t+eUqO+8iToKLSBhcHBlYXIvZW50ZXIvbGVhdmUg5L6n6YeN"
    "4oCc5Ye6546wL+i/m+WFpS/nprvlvIDnmoTliqjkvZzkuI7mlrnlkJHigJ3vvJtzdGF5IOS+p+mH"
    "jeKAnOWcqOWcuuWBnOeVmeKAneOAggotIHBldF9hY3Rpdml0eSDmjIfmnInmmI7noa7mtLvliqjv"
    "vIjotbDliqgv5aWU6LeRL+eOqeiAjS/ll4XmjqLvvInvvJtwZXRfcmVzdCDmjIfkvJHmga/miJbp"
    "nZnmraLvvIjotrTljacv552h6KeJ77yJ77yb5LuF5Zyo5Zy65L2G5peg5piO5pi+5rS75Yqo5pe2"
    "55SoIHBldF9zdGF544CCCi0gbWVtYmVyX2FwcGVhciDnlKjkuo7pppbmrKHlh7rnjrDmiJbnn63m"
    "moLnu4/ov4fvvJvmjIHnu63lnKjlnLrkuJTml6Dov5vlh7rliqjkvZznlKggbWVtYmVyX3N0YXnj"
    "gIIKLSDku4XlvZPml6Dms5XlvZLlhaXku7vkvZXlhbfkvZPnsbvlnovml7bmiY3nlKggb3RoZXLv"
    "vIznpoHmraLnlKggb3RoZXIg5YWc5bqV5bi46KeB5Zy65pmv44CCCmFuYWx5c2lzX25vdGVfdHlw"
    "ZSDmnprkuL49aWRlbnRpdHlfdW5jZXJ0YWlu44CBbG93X3Zpc2liaWxpdHnjgIFub19zaWduaWZp"
    "Y2FudF9ldmVudOOAgW9jY2x1c2lvbuOAgXBvc3NpYmxlX2ZhbHNlX3Bvc2l0aXZl44CBc2NlbmVf"
    "bm9pc2U7IG1lcmdlX3NpbWlsYXJfYWN0aW9ucz10cnVlOyBzcGxpdF9vbmx5X29uX3NpZ25pZmlj"
    "YW50X2NoYW5nZT10cnVlLgppbnRlcnByZXRlZF9zdGF0ZSDlj5fmjqfor43ooajvvIjlv4Xpobvk"
    "u47kuK3pgInmi6nvvIznpoHmraLoh6rpgKDov5HkuYnor43kuI7oi7HmlocgdG9rZW7vvIk6IOWc"
    "qOWcui/mtLvliqgv56e75YqoL+S8keaBry/pnZnmraIv552h55ygL+itpuiniS/pnaDov5Ev56a7"
    "5byALgrmnIDnu4jnuqbmnZ86IOWPquiDvei+k+WHuiBKU09OIOWvueixoTsg5omA5pyJIG9mZnNl"
    "dCDlv4XpobvkuLrpnZ7otJ/mlbDkuJQgZW5kPj1zdGFydDsgb2Zmc2V0IOS7peW9k+WJjei+k+WF"
    "peinhumikeeJh+autei1t+eCueS4uiAwIOenkjsgZXZlbnRfdHlwZSDlv4XpobvmnaXoh6rkuIrp"
    "naLlrprkuYk7IGRldGFpbCDpnIDor6bnu4bmj4/ov7Dlj6/op4Llr5/kuovlrp47IOaJgOacieiH"
    "queEtuivreiogOWtl+auteS9v+eUqOeugOS9k+S4reaWhy4="
).decode("utf-8")

# 当前模板渲染出的 video 模式 user prompt 全文（同样固定输入），逐字节相等。
# 固定指令已全部进入 system prompt；user prompt 只保留逐次变化的上下文。
USER_MODE_SNAPSHOT = base64.b64decode(
    "5a625bqt5LiK5LiL5paH77yaCuWutuW6reamguWGtTog5a625bqt5ZCNPeaIkeeahOWutuW6rTsg"
    "5qCH562+PWhhc19wZXQ7IOWFs+azqOmhuTog5pegOyDpo47moLw9ZmFtaWx5X2NvbXBhbmlvbjsg"
    "6aOO5qC85YGP5aW9PeeugOa0gTsg5Yqp5omL5ZCNPeWutuW6reWKqeaJizsg5aSH5rOoPeWknOmX"
    "tOi+g+WuiemdmS4K5a625bqt5oiQ5ZGYOiDml6AuCuWuoOeJqeS/oeaBrzog5pegLgrovrnnlYw6"
    "IOWutuW6reaho+ahiOS7heS9nOS4uui+heWKqeS4iuS4i+aWh++8jOS4jeiDveabv+S7o+inhumi"
    "keivgeaNri4KCuaRhOWDj+WktOS4iuS4i+aWh++8mgrmkYTlg4/lpLQ957Gz5a62TkFTOyDlkI3n"
    "p7A95a6i5Y6FOyDkvY3nva495a6i5Y6FOyDlpIfms6g955S16KeG5bGP5bmV5Lya5Y+N5YWJLgoK"
    "5Lu75Yqh5LiK5LiL5paH77yaCuS8muivneS4iuS4i+aWhzogc2Vzc2lvbl9pZD0xMDA7IHNvdXJj"
    "ZV9pZD0xOyBzdGFydD0yMDI2LTAzLTEzVDEwOjAwOjAwOyBlbmQ9MjAyNi0wMy0xM1QxMDowMTow"
    "MDsgZHVyYXRpb249NjA7IHNlZ21lbnRfaW5kZXg9MDsgc2VnbWVudF9zdGFydF9vZmZzZXRfc2Vj"
    "PTA7IHNlZ21lbnRfZHVyYXRpb25fc2Vjb25kcz02MC4K562W55Wl5LiK5LiL5paHOiBpbmdlc3Rf"
    "dHlwZT14aWFvbWlfbmFzX2JhY2t1cDsgc291cmNlX3R5cGU9bG9jYWxfZGlyZWN0b3J5OyBzdHJh"
    "dGVneV9ub3RlPeS8mOWFiOW9kue6sy4="
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
CHANGE_RULE_SENTENCE = "15. 变化判定"
OBJECTIVE_SUMMARY_RULE_SENTENCE = "16. 摘要规范"
EMPTY_RESULT_MARKER = "空结果示例"
FRAME_LIST_HEADER = "本批帧时间戳："
VIDEO_OFFSET_CLAUSE = "offset 以当前输入视频片段起点为 0 秒"
IMAGE_OFFSET_CLAUSE = "offset 以本批第 0 帧为 0 秒"


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


def test_video_mode_system_prompt_matches_snapshot() -> None:
    system_prompt, _ = build_video_recognition_prompt(_base_input())

    assert system_prompt == VIDEO_MODE_SNAPSHOT


def test_video_mode_user_prompt_matches_snapshot() -> None:
    _, user_prompt = build_video_recognition_prompt(_base_input())

    assert user_prompt == USER_MODE_SNAPSHOT


def test_video_mode_keeps_original_rule5_rule6_and_has_no_frame_header() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(_base_input())

    assert ORIGINAL_RULE_5 in system_prompt
    assert ORIGINAL_RULE_6 in system_prompt
    assert FRAME_LIST_HEADER not in user_prompt
    # 最终约束中的 offset 子句保持 video 版（位于固定前缀），不出现 image 版。
    assert VIDEO_OFFSET_CLAUSE in system_prompt
    assert IMAGE_OFFSET_CLAUSE not in system_prompt


def test_video_mode_empty_timestamps_is_valid_and_matches_snapshot() -> None:
    # video 模式 + 空 frame_timestamps 必须合法：不抛错，且不渲染帧区段，
    # 渲染结果与改动前快照逐字节一致。
    system_prompt, user_prompt = build_video_recognition_prompt(
        {**_base_input(), "frame_timestamps": []}
    )

    assert system_prompt == VIDEO_MODE_SNAPSHOT
    assert user_prompt == USER_MODE_SNAPSHOT


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

    # image 模式下规则 5/6 换成图像版，且包含 17 事件软上限；video 版规则不再出现。
    assert IMAGE_RULE_5 in system_prompt
    assert IMAGE_RULE_6 in system_prompt
    assert ORIGINAL_RULE_5 not in system_prompt
    assert ORIGINAL_RULE_6 not in system_prompt
    assert "17. 事件数量上限" in system_prompt
    assert EVENT_CAP_SENTENCE in system_prompt
    # 最终约束中的 offset 子句换成 image 版，与系统提示规则 6 不再冲突。
    assert IMAGE_OFFSET_CLAUSE in system_prompt
    assert VIDEO_OFFSET_CLAUSE not in system_prompt

    # 16 个事件类型名必须全部保留在固定前缀（system prompt）中，且固定规则
    # 不再重复出现在逐次变化的 user prompt 里（否则公共前缀会被变量切断）。
    for event_type in sorted(VIDEO_EVENT_TYPES):
        assert event_type in system_prompt
    assert "event_type 定义" not in user_prompt
    assert "最终约束" not in user_prompt


def test_change_detection_and_empty_result_guidance_are_rendered() -> None:
    # 静止误报的核心 prompt 修复：显式允许空 events，并要求先做帧间变化判定。
    for input_mode in ("video", "image"):
        overrides: dict = {"input_mode": input_mode}
        if input_mode == "image":
            overrides["frame_timestamps"] = [0.0, 3.5, 7.0]
        system_prompt, _ = build_video_recognition_prompt({**_base_input(), **overrides})

        assert CHANGE_RULE_SENTENCE in system_prompt
        assert OBJECTIVE_SUMMARY_RULE_SENTENCE in system_prompt
        assert EMPTY_RESULT_MARKER in system_prompt
        assert '"events": []' in system_prompt
        assert "no_significant_event" in system_prompt
        # 静止不再是“需要忽略”的偏置：它现在是受控状态词，且规则 15 明确它本身不是事件。
        assert "“静止”“休息”本身不是事件" in system_prompt
        assert "不要因短暂静止忽略整段价值" not in system_prompt


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


def test_image_mode_empty_timestamps_raises() -> None:
    # image 模式空 frame_timestamps 属于调用方错误，必须在渲染前响亮失败，
    # 而不是静默渲染出一个只有表头的空区段；错误信息必须点名缺失的输入。
    with pytest.raises(ValueError, match="frame timestamp"):
        build_video_recognition_prompt(
            {
                **_base_input(),
                "input_mode": "image",
                "frame_timestamps": [],
            }
        )


def test_image_mode_non_monotonic_timestamps_raise() -> None:
    # 时间戳乱序属于调用方错误，必须抛出 ValueError 并指明出错的下标。
    with pytest.raises(ValueError) as excinfo:
        build_video_recognition_prompt(
            {
                **_base_input(),
                "input_mode": "image",
                "frame_timestamps": [1.0, 0.5],
            }
        )
    assert "index 1" in str(excinfo.value)


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


def test_compiler_image_mode_empty_timestamps_raises() -> None:
    # 编译器路径（dataclass 构造 + compiler）在 image 模式空列表时同样必须响亮失败，
    # 保证编译器校验无法被绕过。
    with pytest.raises(ValueError, match="frame timestamp"):
        compile_video_recognition_prompt(_compiler_input(input_mode="image", frame_timestamps=[]))


def _example_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return objects
        try:
            payload, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(payload, dict):
            objects.append(payload)
        index = end


def test_rule_18_naming_consistency_is_rendered_in_both_modes() -> None:
    for input_mode in ("video", "image"):
        overrides: dict = {"input_mode": input_mode}
        if input_mode == "image":
            overrides["frame_timestamps"] = [0.0, 3.5, 7.0]
        system_prompt, _ = build_video_recognition_prompt({**_base_input(), **overrides})

        assert "18. 称谓一致" in system_prompt
        assert "禁止改用“一只/这只 + 物种”写法" in system_prompt
        assert "candidate_profile_names" in system_prompt


def test_example_entities_use_profile_name_across_title_summary_detail() -> None:
    system_prompt, _ = build_video_recognition_prompt(_base_input())

    checked = 0
    for example in _example_objects(system_prompt):
        for event in example.get("events") or []:
            for entity in event.get("related_entities") or []:
                name = entity.get("matched_profile_name")
                if not name:
                    continue
                checked += 1
                assert name in event["title"]
                assert name in event["summary"]
                assert name in event["detail"]
    assert checked >= 2


def test_schema_examples_include_candidate_profile_names() -> None:
    system_prompt, _ = build_video_recognition_prompt(_base_input())

    candidates = [
        entity.get("candidate_profile_names")
        for example in _example_objects(system_prompt)
        for event in example.get("events") or []
        for entity in event.get("related_entities") or []
        if entity.get("matched_profile_name") is None
    ]
    assert candidates == [["墨墨", "嘟嘟"]]


def test_interpreted_state_examples_stay_within_controlled_vocabulary() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(_base_input())

    for value in INTERPRETED_STATES:
        assert value in system_prompt

    used: list[str] = []
    for example in _example_objects(system_prompt):
        for event in example.get("events") or []:
            used.extend(event.get("interpreted_state") or [])
    assert used
    assert set(used) <= set(INTERPRETED_STATES)


def test_continuity_block_renders_only_with_previous_subjects() -> None:
    _, without = build_video_recognition_prompt(_base_input())
    _, with_subjects = build_video_recognition_prompt(
        {**_base_input(), "previous_subjects": ["嘟嘟", "墨墨"]}
    )

    assert "相邻片段上下文" not in without
    assert "上一段在场主体=嘟嘟、墨墨" in with_subjects

from django.shortcuts import render, redirect
#from .models import Project
from .models import Contact
import mysql.connector
from django.shortcuts import render
from django.http import JsonResponse
from django.conf import settings
from django.core.mail import send_mail
from django.conf import settings


def index(request):
    return render(request, "portfolio/index.html")

def about(request):
    return render(request, "portfolio/about.html")

def portfolio(request):
    projects = Contact.objects.all()
    return render(request, "portfolio/portfolio.html", {"Contact": Contact})

def contact_view(request):
    if request.method == "POST":
        print("📩 有人送出表單了！")  # Debug
        name = request.POST.get("name")
        email = request.POST.get("email")
        message = request.POST.get("message")

        try:
            Contact.objects.create(
                name=name,
                email=email,
                message=message
            )
            print("✅ 已存進資料庫")  # Debug

            send_mail(
                subject=f"網站聯絡表單來自 {name}",
                message=f"姓名: {name}\nEmail: {email}\n\n訊息:\n{message}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=["r0983516925@gmail.com"],
            )
            print("📨 信件寄出")  # Debug

            return redirect("/contact/?success=1")
        except Exception as e:
            print("❌ 錯誤：", e)  # Debug
            return redirect("/contact/?error=1")

    success = request.GET.get("success") == "1"
    error = request.GET.get("error") == "1"
    return render(request, "portfolio/contact.html", {"success": success, "error": error})




def query_db(db_name, sql, params=None):
    conn = mysql.connector.connect(
        host=settings.MYSQL_CONFIG["host"],
        port=settings.MYSQL_CONFIG["port"],
        user=settings.MYSQL_CONFIG["user"],
        password=settings.MYSQL_CONFIG["password"],
        database=db_name,
    )
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params or [])
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def minecraft_view(request):
    return render(request, "portfolio/minecraft.html")

def minecraft_search(request):
    player_id = request.GET.get("player_id")

    # mmocore
    mmocore_sql = """
        SELECT uuid, class, level,
               mmocore_playerdata.professions->"$.mainlevel.level" AS mainlevel_level
        FROM mmocore_playerdata
        WHERE uuid = %s
    """
    mmocore_data = query_db("mmocore", mmocore_sql, (player_id,))

    # playerdata
    playerdata_sql = """
        SELECT player_uuid, player_name, money
        FROM mpdb_economy
        WHERE player_name = %s
    """
    playerdata_data = query_db("playerdata", playerdata_sql, (player_id,))

    # guilds
    guild_sql = """
        SELECT data->'$.name' AS gname,
               data->'$.tier.level' AS glevel,
               data->'$.balance' AS gmoney,
               data->'$.guildMaster.uuid' AS master_uuid
        FROM guilds_guild
    """
    guilds_data = query_db("guilds", guild_sql)
    guilds_data = [g for g in guilds_data if g["master_uuid"] == player_id]

    # CMI
    cmi_sql = """
        SELECT player_uuid, username, TotalPlayTime, Balance, FlightCharge, DisplayName
        FROM cmi_users
        WHERE username = %s OR player_uuid = %s
    """
    cmi_data = query_db("CMI", cmi_sql, (player_id, player_id))

    return JsonResponse({
        "mmocore": mmocore_data,
        "playerdata": playerdata_data,
        "guilds": guilds_data,
        "cmi": cmi_data,
    })

def minecraft_rank(request):
    rank_type = request.GET.get("type", "money")  # 預設查金幣排行
    rows = []

    if rank_type == "money":  # 金幣排行
        sql = """
            SELECT player_name, money
            FROM mpdb_economy
            ORDER BY money DESC
            LIMIT 50
        """
        rows = query_db("playerdata", sql)

    elif rank_type == "level":  # 等級排行
        sql = """
            SELECT uuid, class, level,
                   professions->'$.mainlevel.level' AS mainlevel_level
            FROM mmocore_playerdata
            ORDER BY mainlevel_level DESC
            LIMIT 50
        """
        rows = query_db("mmocore", sql)

    elif rank_type == "guild":  # 公會排行
        sql = """
            SELECT data->'$.name' AS gname,
                   data->'$.tier.level' AS glevel,
                   data->'$.balance' AS gmoney
            FROM guilds_guild
            ORDER BY CAST(data->'$.balance' AS UNSIGNED) DESC
            LIMIT 50
        """
        rows = query_db("guilds", sql)

    elif rank_type == "playtime":  # 遊玩時間排行
        sql = """
            SELECT username, TotalPlayTime, Balance
            FROM cmi_users
            WHERE lastLoginTime != 0
            ORDER BY TotalPlayTime DESC
            LIMIT 50
        """
        rows = query_db("CMI", sql)

    return JsonResponse({"rank_type": rank_type, "rows": rows})

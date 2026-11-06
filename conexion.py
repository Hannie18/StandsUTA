import pymysql
from pymysql.cursors import DictCursor

def obtener_conexion():
    return pymysql.connect(
        host="mysql-ce66135-hanna181207-4015.d.aivencloud.com",
        user="avnadmin",
        password="AVNS_8CQQ5abeTpCitG_hYeh", # Pon tu contraseña real aquí
        database="defaultdb",
        port=11161,
        charset="utf8mb4",
        ssl={"ssl": {}},
        cursorclass=pymysql.cursors.DictCursor
    )
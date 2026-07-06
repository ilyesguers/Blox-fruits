from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# قائمة لتخزين الفواكه المكتشفة حالياً
fruits_database = []

class FruitData(BaseModel):
    name: str
    position: dict # {x, y, z}
    server_id: str

@app.post("/report_fruit")
async def report_fruit(data: FruitData):
    fruits_database.append(data.dict())
    return {"message": "Fruit recorded"}

@app.get("/get_fruits")
async def get_fruits():
    return fruits_database

@app.get("/")
async def root():
    return {"status": "Architect Server Online"}

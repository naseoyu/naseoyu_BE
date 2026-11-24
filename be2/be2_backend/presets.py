from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .db import Preset

def seed(database):
    with database.session() as db:
        insert=sqlite_insert if db.bind.dialect.name=='sqlite' else pg_insert
        for key,gpu,cpu,tb in [('small',4,2,10),('medium',16,8,50),('large',64,32,200)]:
            payload={'name':key+' 데이터센터 예시','inputs':{
                'gpu_count':gpu,'cpu_server_count':cpu,'storage_tb':tb,
                'pue':'1.5','evaluation_months':12,'data_basis':'illustrative'}}
            db.execute(insert(Preset).values(id=key,revision=1,payload=payload).on_conflict_do_nothing(index_elements=['id']))
        db.commit()

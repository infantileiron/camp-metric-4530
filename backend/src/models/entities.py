from sqlalchemy import Column,Integer,String,Float,JSON,Text,Boolean,ForeignKey,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Metric(Base):
 __tablename__='metrics';__table_args__=(UniqueConstraint('code','version'),)
 id=Column(Integer,primary_key=True);code=Column(String(40),nullable=False);version=Column(Integer,nullable=False);name=Column(String(100));numerator=Column(String(200));denominator=Column(String(200));numerator_mode=Column(String(20));denominator_mode=Column(String(20));note=Column(Text,default='');locked=Column(Boolean,default=False)
class Fact(Base):
 __tablename__='facts';__table_args__=(UniqueConstraint('class_code','day'),)
 id=Column(Integer,primary_key=True);class_code=Column(String(50),nullable=False);day=Column(String(10),nullable=False);mode=Column(String(20));enrolled=Column(Integer);attended=Column(Integer);completed=Column(Integer);refunds=Column(Integer);note=Column(Text,default='')
class Snapshot(Base):
 __tablename__='snapshots'
 id=Column(Integer,primary_key=True);metric_id=Column(Integer,ForeignKey('metrics.id'));baseline_id=Column(Integer,ForeignKey('snapshots.id'),nullable=True);date_from=Column(String(10));date_to=Column(String(10));state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='');note=Column(Text,default='');approval=Column(String(20),default='draft');approved_by=Column(String(100),default='')
class Delivery(Base):
 __tablename__='deliveries';__table_args__=(UniqueConstraint('package_id'),UniqueConstraint('snapshot_id'),UniqueConstraint('metric_code','date_from','date_to','revision'))
 id=Column(Integer,primary_key=True);package_id=Column(String(64),nullable=False);snapshot_id=Column(Integer,ForeignKey('snapshots.id'),nullable=False)
 metric_code=Column(String(40),nullable=False);metric_version=Column(Integer);date_from=Column(String(10),nullable=False);date_to=Column(String(10),nullable=False)
 revision=Column(Integer,nullable=False);approved_at=Column(String(40));approved_by=Column(String(100));checksum=Column(String(64),nullable=False);payload=Column(JSON,nullable=False)
 status=Column(String(20),default='pending');outcome=Column(String(64),default='');attempts=Column(Integer,default=0);last_error=Column(Text,default='')
 ack=Column(JSON,nullable=True);confirmed_at=Column(String(40),default='');created_at=Column(String(40));updated_at=Column(String(40))
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))

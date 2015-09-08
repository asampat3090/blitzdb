import time
import copy
import sqlalchemy

from blitzdb.queryset import QuerySet as BaseQuerySet
from functools import wraps
from sqlalchemy.sql import select,func,expression,delete,distinct,and_,union,intersect
from sqlalchemy.sql.expression import join,asc,desc,outerjoin
from ..file.serializers import JsonSerializer
from .helpers import set_value
from collections import OrderedDict

class QuerySet(BaseQuerySet):

    def __init__(self, backend, table, cls,
                 condition = None,
                 select = None,
                 intersects = None,
                 raw = False,
                 include = None,
                 only = None,
                 joins = None,
                 extra_fields = None,
                 group_bys = None,
                 objects = None,
                 havings = None,
                 limit = None,
                 offset = None
                 ):
        super(QuerySet,self).__init__(backend = backend,cls = cls)

        self.joins = joins
        self.backend = backend
        self.condition = condition
        self.select = select
        self.havings = havings
        self.only = only
        self.include = include
        self.extra_fields = extra_fields
        self.group_bys = group_bys
        self.cls = cls
        self._limit = limit
        self._offset = offset
        self.table = table
        self._raw = raw
        self.intersects = intersects
        self.objects = objects
        if self.objects:
            self.pop_objects = self.objects[:]

        self._it = None
        self.order_bys = None
        self.count = None
        self.result = None

    def limit(self,limit):
        self._limit = limit
        return self

    def offset(self,offset):
        self._offset = offset
        return self

    def deserialize(self, data):

        d,lazy = self.backend.deserialize_db_data(data)

        if self._raw:
            return d

        deserialized_attributes = self.backend.deserialize(d)
        obj = self.backend.create_instance(self.cls, deserialized_attributes,lazy = lazy)

        return obj

    def sort(self, keys,direction = None):
        #we sort by a single argument
        if direction:
            keys = ((keys,direction),)
        order_bys = []
        for key,direction in keys:
            if direction > 0:
                direction = asc
            else:
                direction = desc
            try:
                column = self.backend.get_column_for_key(self.cls,key)
            except KeyError:
                raise AttributeError("Attempting to sort results by a non-indexed field %s" % key)
            order_bys.append(direction(column))
        self.order_bys = order_bys
        self.objects = None
        return self

    def next(self):
        if self._it is None:
            self._it = iter(self)
        return self._it.next()

    __next__ = next

    def __iter__(self):
        if self.objects is None:
            self.get_objects()
        for obj in self.objects:
            yield self.deserialize(obj)
        raise StopIteration

    def __contains__(self, obj):
        pks = self.distinct_pks()
        if isinstance(obj, list) or isinstance(obj, tuple):
            obj_list = obj
        else:
            obj_list = [obj]
        for obj in obj_list:
            if obj.pk not in pks:
                return False
        return True

    def get_objects(self):

        s = self.get_select()

        #We create a CTE, which will allow us to join the required includes.
        s_cte = s.cte("results")
        rows = []
        joins = []
        keymap = {}

        def join_table(collection,table,key,params,path = None):
            if path is None:
                path = []
            if 'relationship_table' in params['relation']:
                join_many_to_many(collection,table,key,params,path)
            else:
                join_foreign_key(collection,table,key,params,path)

        def process_fields_and_subkeys(related_collection,related_table,params,path):

            params['table_fields'] = {}
            for field,column_name in params['fields'].items():
                column_label = '_'.join(path+[column_name])
                params['table_fields'][field] = column_label
                rows.append(related_table.c[column_name].label(column_label))

            for subkey,subparams in sorted(params['joins'].items(),key = lambda i : i[0]):
                join_table(params['collection'],related_table,subkey,subparams,path = path)

        def join_foreign_key(collection,table,key,params,path):
            related_table = params['table'].alias()
            related_collection = params['relation']['collection']
            condition = table.c[params['relation']['column']] == related_table.c.pk
            joins.append((related_table,condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+\
                                        [params['relation']['column']])

        def join_many_to_many(collection,table,key,params,path):
            relationship_table = params['relation']['relationship_table'].alias()
            related_collection = params['relation']['collection']
            related_table = self.backend.get_collection_table(related_collection).alias()
            left_condition = relationship_table.c['pk_%s' % collection] == table.c.pk
            right_condition = relationship_table.c['pk_%s' % related_collection] == related_table.c.pk
            joins.append((relationship_table,left_condition))
            joins.append((related_table,right_condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+[key])


        def build_field_map(params,path = None,current_map = None):

            def m2m_getter(join_params,name,pk_key):

                def f(d,obj):
                    pk_value = obj[pk_key]
                    if not name in d:
                        d[name] = OrderedDict()
                    if pk_value is None:
                        return None
                    if not pk_value in d[name]:
                        d[name][pk_value] = {}
                    if not '__lazy__' in d[name][pk_value]:
                        d[name][pk_value]['__lazy__'] = join_params['lazy']
                    return d[name][pk_value]

                return f

            def fk_getter(join_params,key):

                def f(d,obj):
                    pk_value = obj[join_params['table_fields']['pk']]
                    if pk_value is None:
                        return None
                    if not key in d or d[key] is None:
                        d[key] = {}
                    if not '__lazy__' in d[key]:
                        d[key]['__lazy__'] = join_params['lazy']
                    return d[key]

                return f

            if current_map is None:
                current_map = {}
            if path is None:
                path = []
            for key,field in params['table_fields'].items():
                current_map[field] = path+[key]
            for name,join_params in params['joins'].items():
                if name in current_map:
                    del current_map[name]
                if 'relationship_table' in join_params['relation']:
                    build_field_map(join_params,path+[m2m_getter(join_params,name,
                                                                 join_params['table_fields']['pk'])],current_map)
                else:
                    build_field_map(join_params,path+[fk_getter(join_params,name),],current_map)
            return current_map

        def replace_ordered_dicts(d):
            for key,value in d.items():
                if isinstance(value,OrderedDict):
                    replace_ordered_dicts(value)
                    d[key] = list(value.values())
                elif isinstance(value,dict):
                    d[key] = replace_ordered_dicts(value)
            return d

        if self.include:
            include = copy.deepcopy(self.include)
            if not isinstance(include,(list,tuple)):
                raise AttributeError("include must be a list/tuple")
        else:
            include = ()

        exclude = []
        if self.only:
            if isinstance(self.only,dict):
                only = []
                for key,value in self.only.items():
                    if value is False:
                        exclude.append(key)
                    else:
                        only.append(key)
            else:
                only = set(self.only)
            include = set(include)
            for only_key in only:
                include.add(only_key)

        self.include_joins = self.backend.get_include_joins(self.cls,includes = include,excludes = exclude)

        process_fields_and_subkeys(self.include_joins['collection'],s_cte,self.include_joins,[])

        if self.order_bys:
            order_bys = self.order_bys[:]
        else:
            order_bys = ['pk']

        if joins:
            for i,j in enumerate(joins):
                order_bys.append(j[0])
                s_cte = s_cte.outerjoin(*j)

        with self.backend.transaction(use_auto = False):
            try:
                result = self.backend.connection.execute(select(rows).select_from(s_cte).order_by(*order_bys))
                if result.returns_rows:
                    objects = list(result.fetchall())
                else:
                    objects = []
            except sqlalchemy.exc.ResourceClosedError:
                objects = None
                raise

        #we "fold" the objects back into one list structure
        self.objects = []
        pks = []
        field_map = build_field_map(self.include_joins)

        unpacked_objects = OrderedDict()
        for obj in objects:
            if not obj['pk'] in unpacked_objects:
                unpacked_objects[obj['pk']] = {'__lazy__' : self.include_joins['lazy']}
            unpacked_obj = unpacked_objects[obj['pk']]
            for key,path in field_map.items():
                d = unpacked_obj
                for element in path[:-1]:
                    if callable(element):
                        d = element(d,obj)
                        if d is None:
                            break
                    else:
                        d = d[element]
                else:
                    d[path[-1]] = obj[key]

        self.objects = [replace_ordered_dicts(unpacked_obj) for unpacked_obj in unpacked_objects.values()]
        self.pop_objects = self.objects[:]

    def as_list(self):
        if self.objects is None:
            self.get_objects()
        return [self.deserialize(obj) for obj in self.objects]

    def __getitem__(self,key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step
            if step != None:
                raise IndexError("SQL backend dos not support steps in slices")
            if key.start == None:
                start = 0
            if key.stop == None:
                stop = len(self)
            if start < 0:
                start = len(self) + start
            if stop < 0:
                stop = len(self) + stop
            qs = copy.copy(self)
            if start:
                qs.offset(start)
            qs.limit(stop-start)
            qs.objects = None
            qs.count = None
            return qs
        if self.objects is None:
            self.get_objects()
        return self.deserialize(self.objects[key])

    def pop(self,i = 0):
        if self.objects is None:
            self.get_objects()
        if self.pop_objects:
            return self.deserialize(self.pop_objects.pop())
        raise IndexError("pop from empty list")

    def filter(self,*args,**kwargs):
        qs = self.backend.filter(self.cls,*args,**kwargs)
        return self.intersect(qs)

    def intersect(self,qs):
        new_qs = QuerySet(self.backend,self.table,self.cls,select = intersect(self.get_select(),qs.get_select()))
        return new_qs

    def delete(self):
        with self.backend.transaction(use_auto = False):
            delete_stmt = self.table.delete().where(self.table.c.pk.in_(self.get_select(fields = [self.table.c.pk])))
            self.backend.connection.execute(delete_stmt)

    def get_fields(self):
        return [self.table]

    def get_select(self,fields = None):
        if self.select is not None:
            return self.select
        if fields is None:
            fields = self.get_fields()
        if self.extra_fields:
            fields.extend(self.extra_fields)
        s = select(fields)
        if self.joins:
            full_join = None
            for j in self.joins:
                if full_join is not None:
                    full_join = full_join.join(*j)
                else:
                    full_join = outerjoin(self.table,*j)
            s = s.select_from(full_join)

        if self.condition is not None:
            s = s.where(self.condition)
        if self.group_bys:
            s = s.group_by(*self.group_bys)
        if self.havings:
            for having in self.havings:
                s = s.having(having)
        if self.order_bys:
            s = s.order_by(*self.order_bys)
        if self._offset:
            s = s.offset(self._offset)
        if self._limit:
            s = s.limit(self._limit)
        return s

    def __len__(self):
        if self.count is None:
            if self.objects is not None:
                self.count = len(self.objects)
            else:
                with self.backend.transaction(use_auto = False):
                    s = select([func.count()]).select_from(self.get_select(fields = [self.table.c.pk]).alias('count_select'))
                    result = self.backend.connection.execute(s)
                    self.count = result.first()[0]
                    result.close()
        return self.count

    def distinct_pks(self):
        with self.backend.transaction(use_auto = False):
            s = self.get_select([self.table.c.pk]).distinct(self.table.c.pk)
            result = self.backend.connection.execute(s)
            return set([r[0] for r in result.fetchall()])
        
    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __eq__(self, other):
        if isinstance(other, QuerySet): 
            if self.cls == other.cls and len(self) == len(other) \
              and self.distinct_pks() == other.distinct_pks():
                return True
        elif isinstance(other, list):
            if len(other) != len(self.keys):
                return False
            objs = list(self)
            if other == objs:
                return True
        return False


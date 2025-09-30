class FindRouter:
    """
    把 Find app 的資料寫到 find_db
    """
    def db_for_read(self, model, **hints):
        if model._meta.app_label == 'Find':
            return 'find_db'
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == 'Find':
            return 'find_db'
        return None

    def allow_relation(self, obj1, obj2, **hints):
        if obj1._meta.app_label == 'Find' or obj2._meta.app_label == 'Find':
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == 'Find':
            return db == 'find_db'
        return None

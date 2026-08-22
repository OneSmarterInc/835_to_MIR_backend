import os
import django
from django.apps import apps
from tabulate import tabulate

def verify_counts():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project835.settings")
    django.setup()
    
    models = apps.get_models()
    table_data = []
    
    print("--- POSTGRESQL MIGRATION VERIFICATION ---")
    print(f"Connected to DB Engine: {django.conf.settings.DATABASES['default']['ENGINE']}")
    print("-" * 50)
    
    total_records = 0
    for model in models:
        count = model.objects.count()
        if count > 0:
            table_data.append([model.__name__, count])
            total_records += count
            
    print(tabulate(table_data, headers=["Model", "Record Count"]))
    print("-" * 50)
    print(f"Total Records Migrated: {total_records}")
    print("Ensure these counts match your SQLite backup.")

if __name__ == "__main__":
    verify_counts()

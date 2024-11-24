# Generated by Django 5.1.2 on 2024-11-21 21:56

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("players", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="player",
            name="profileColor",
            field=models.IntegerField(
                choices=[(1, "Red"), (2, "Blue"), (3, "Green"), (4, "Yellow")],
                default=0,
            ),
        ),
    ]
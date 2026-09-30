from dataclasses import dataclass
from types import SimpleNamespace


@dataclass
class FakeItem:
    attributes: dict
    secret: bytes
    secret_content_type: str
    deleted: bool = False
    locked: bool = False

    def get_secret(self):
        return self.secret

    def get_attributes(self):
        return self.attributes

    def is_locked(self):
        return self.locked

    def delete(self):
        self.deleted = True


class FakeCollection:
    def __init__(self):
        self.session = SimpleNamespace(encrypted=True)
        self.items = []
        self.locked = False

    def is_locked(self):
        return self.locked

    def search_items(self, attributes):
        return iter(
            i for i in self.items if not i.deleted and i.attributes == attributes
        )

    def create_item(
        self, label, attributes, secret, replace=False, content_type="text/plain"
    ):
        if replace:
            for item in self.search_items(attributes):
                item.delete()
        item = FakeItem(attributes, secret, content_type)
        self.items.append(item)
        return item

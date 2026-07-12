"""Perceptual dHash gate behavior with synthetic surfaces."""
import unittest

import lms_cover_display as mod


def _pygame():
    import pygame
    pygame.init()
    return pygame


def grad(pygame, seed, size):
    s = pygame.Surface((size, size))
    for y in range(size):
        for x in range(size):
            s.set_at((x, y), (((x * 7 + seed * 37) % 256),
                              ((y * 5 + seed * 11) % 256),
                              ((x + y + seed * 53) % 256)))
    return s


class TestDhash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = _pygame()
        cls.big = grad(cls.pg, 1, 240)

    def test_same_art_across_sizes(self):
        small = self.pg.transform.smoothscale(self.big, (60, 60))
        d = mod._hamming(mod._dhash(self.pg, self.big),
                         mod._dhash(self.pg, small))
        self.assertLessEqual(d, 6)

    def test_different_art_far(self):
        other = grad(self.pg, 9, 240)
        d = mod._hamming(mod._dhash(self.pg, self.big),
                         mod._dhash(self.pg, other))
        self.assertGreater(d, 16)

    def test_logo_vs_cover_far(self):
        logo = self.pg.Surface((200, 200))
        logo.fill((240, 240, 240))
        self.pg.draw.rect(logo, (200, 30, 30), (40, 80, 120, 40))
        d = mod._hamming(mod._dhash(self.pg, self.big),
                         mod._dhash(self.pg, logo))
        self.assertGreater(d, 16)

    def test_center_crop_equivalence(self):
        wide = self.pg.Surface((300, 200))
        wide.blit(self.pg.transform.smoothscale(self.big, (200, 200)), (50, 0))
        d = mod._hamming(mod._dhash(self.pg, wide),
                         mod._dhash(self.pg, self.big))
        self.assertLessEqual(d, 8)


if __name__ == "__main__":
    unittest.main()

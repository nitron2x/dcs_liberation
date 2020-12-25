from typing import Optional

from PySide2.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout

from game import Game


class QIntelBox(QGroupBox):
    def __init__(self, game: Game) -> None:
        super().__init__("Intel")
        self.game = game

        columns = QHBoxLayout()
        self.setLayout(columns)

        summary = QVBoxLayout()
        columns.addLayout(summary)

        self.total_aircraft = QLabel()
        summary.addWidget(self.total_aircraft)
        self.total_ground_forces = QLabel()
        summary.addWidget(self.total_ground_forces)

        self.update_summary()

    def set_game(self, game: Optional[Game]) -> None:
        self.game = game
        self.update_summary()

    def update_summary(self) -> None:
        if self.game is None:
            aircraft = 0
            ground_units = 0
        else:
            data = self.game.game_stats.data_per_turn[-1]
            aircraft = data.enemy_units.aircraft_count
            ground_units = data.enemy_units.vehicles_count
        self.total_aircraft.setText(f"Total enemy aircraft: {aircraft}")
        self.total_ground_forces.setText(
            f"Total enemy ground units: {ground_units}")